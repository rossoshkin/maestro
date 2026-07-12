"""Reviewer Role runtime over immutable Artifacts."""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

from pydantic import Field, ValidationError

from maestro.application.artifacts import ArtifactService
from maestro.application.controllers import observe_generation, with_condition
from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactProducer,
    ArtifactRepository,
    ArtifactStorage,
    ArtifactStorageError,
    ArtifactType,
    ArtifactWorkItemReference,
)
from maestro.domain.events import (
    EventDraft,
    EventExecutionReference,
    EventPayload,
    EventPublisher,
)
from maestro.domain.exceptions import ResourceNotFoundError
from maestro.domain.providers import (
    ModelProvider,
    Provider,
    ProviderMessage,
    ProviderMessageRole,
    ProviderOperationError,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)
from maestro.domain.resources import (
    ConditionStatus,
    MaestroModel,
    ResourceName,
    ResourceReference,
    utc_now,
)
from maestro.domain.reviews import (
    Review,
    ReviewArtifactReference,
    ReviewFinding,
    ReviewPhase,
    ReviewRepository,
    ReviewStatus,
    ReviewVerdict,
)

REVIEWER_RUNTIME = "reviewer-runtime"
REVIEWER_PROMPT_TEMPLATE = """You are Maestro's Reviewer Role.

Evaluate only the immutable Artifacts supplied in this prompt.
Do not edit files, run commands, infer from hidden workspace state, or approve on
behalf of a human. Base the verdict on recorded evidence, acceptance criteria,
and the Review policy. Return only a compact JSON object matching the Review
output schema.
"""
REVIEWER_DENIED_CAPABILITIES = (
    "filesystem.write",
    "filesystem.edit",
    "shell.execute",
    "shell.execute.test",
    "git.push",
    "workflow.transition",
    "approval.decide",
)


class PackagedReviewArtifact(MaestroModel):
    """Immutable Artifact content supplied to a Reviewer."""

    ref: ReviewArtifactReference
    artifact_type: ArtifactType = Field(alias="type")
    media_type: str = Field(alias="mediaType")
    sha256: str
    content: str


class ReviewerInput(MaestroModel):
    """Provider-independent Reviewer input package."""

    review_ref: ResourceReference = Field(alias="reviewRef")
    work_item_ref: ResourceReference = Field(alias="workItemRef")
    acceptance_criteria: tuple[str, ...] = Field(alias="acceptanceCriteria")
    policy: dict[str, Any]
    capabilities: dict[str, tuple[str, ...]]
    subject_artifacts: tuple[PackagedReviewArtifact, ...] = Field(
        alias="subjectArtifacts"
    )


class ReviewerOutput(MaestroModel):
    """Structured output expected from a Reviewer provider."""

    verdict: ReviewVerdict
    summary: str
    blocking_findings: tuple[ReviewFinding, ...] = Field(
        default_factory=tuple,
        alias="blockingFindings",
    )
    non_blocking_findings: tuple[ReviewFinding, ...] = Field(
        default_factory=tuple,
        alias="nonBlockingFindings",
    )
    missing_evidence: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="missingEvidence",
    )


class ReviewerInvocationStatus(MaestroModel):
    """Review runtime outcome."""

    verdict: ReviewVerdict | None = None
    failed: bool = False
    failure_message: str = Field(default="", alias="failureMessage")


class ReviewerInvocationResult(MaestroModel):
    """Result of one Reviewer invocation."""

    review_ref: ResourceReference = Field(alias="reviewRef")
    status: ReviewerInvocationStatus
    prompt_artifact_ref: ResourceReference | None = Field(
        default=None,
        alias="promptArtifactRef",
    )
    response_artifact_ref: ResourceReference | None = Field(
        default=None,
        alias="responseArtifactRef",
    )
    review_artifact_ref: ResourceReference | None = Field(
        default=None,
        alias="reviewArtifactRef",
    )


class ReviewerRuntime:
    """Invoke a Reviewer provider and persist Review evidence."""

    def __init__(
        self,
        *,
        review_repository: ReviewRepository,
        artifact_repository: ArtifactRepository,
        artifact_storage: ArtifactStorage,
        artifact_service: ArtifactService,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._review_repository = review_repository
        self._artifact_repository = artifact_repository
        self._artifact_storage = artifact_storage
        self._artifact_service = artifact_service
        self._event_publisher = event_publisher

    async def invoke_review(
        self,
        review_id: UUID,
        *,
        provider: Provider,
        runtime: ModelProvider,
        model: str,
    ) -> ReviewerInvocationResult:
        """Run a Review against immutable subject Artifacts."""

        review = await self._review_repository.get(review_id)
        running = await self._mark_running(review)
        packaged_artifacts, missing_evidence = await self._package_subject_artifacts(
            running
        )
        if missing_evidence:
            return await self._complete_unable_to_review(running, missing_evidence)

        reviewer_input = build_reviewer_input(running, packaged_artifacts)
        prompt = _review_prompt(reviewer_input)
        prompt_artifact = await self._create_artifact(
            running,
            name=f"review-prompt-{running.metadata.id.hex[:12]}",
            artifact_type=ArtifactType.PROMPT,
            media_type="text/markdown",
            content=prompt.encode("utf-8"),
            source_refs=_subject_resource_refs(running),
        )

        try:
            response = await runtime.generate_structured(
                StructuredGenerationRequest(
                    model=model,
                    messages=(
                        ProviderMessage(
                            role=ProviderMessageRole.SYSTEM,
                            content=REVIEWER_PROMPT_TEMPLATE,
                        ),
                        ProviderMessage(role=ProviderMessageRole.USER, content=prompt),
                    ),
                    responseSchema=ReviewerOutput.model_json_schema(by_alias=True),
                    timeoutSeconds=provider.spec.timeout_seconds,
                )
            )
        except ProviderOperationError as error:
            failed = await self._mark_failed(
                running,
                f"{error.failure.code}: {error.failure.message}",
            )
            await self._publish_review_event("ReviewFailed", failed)
            return ReviewerInvocationResult(
                reviewRef=_resource_ref(failed),
                status=ReviewerInvocationStatus(
                    failed=True,
                    failureMessage=failed.status.failure_message,
                ),
                promptArtifactRef=_resource_ref(prompt_artifact),
            )

        response_artifact = await self._create_response_artifact(
            running,
            response,
            source_refs=(_resource_ref(prompt_artifact),),
        )
        try:
            output = ReviewerOutput.model_validate(response.output)
            review_status = _review_status_from_output(output)
        except (ValidationError, ValueError) as error:
            failed = await self._mark_failed(running, f"ReviewOutputInvalid: {error}")
            await self._publish_review_event("ReviewFailed", failed)
            return ReviewerInvocationResult(
                reviewRef=_resource_ref(failed),
                status=ReviewerInvocationStatus(
                    failed=True,
                    failureMessage=failed.status.failure_message,
                ),
                promptArtifactRef=_resource_ref(prompt_artifact),
                responseArtifactRef=_resource_ref(response_artifact),
            )

        review_artifact = await self._create_artifact(
            running,
            name=f"review-output-{running.metadata.id.hex[:12]}",
            artifact_type=ArtifactType.REVIEW,
            media_type="application/json",
            content=_json_bytes(output.model_dump(mode="json", by_alias=True)),
            source_refs=(
                *_subject_resource_refs(running),
                _resource_ref(prompt_artifact),
                _resource_ref(response_artifact),
            ),
        )
        completed = await self._review_repository.update_status(
            running.metadata.id,
            _with_review_condition(running, review_status),
            expected_resource_version=running.metadata.resource_version,
        )
        await self._publish_review_event("ReviewCompleted", completed)
        return ReviewerInvocationResult(
            reviewRef=_resource_ref(completed),
            status=ReviewerInvocationStatus(verdict=completed.status.verdict),
            promptArtifactRef=_resource_ref(prompt_artifact),
            responseArtifactRef=_resource_ref(response_artifact),
            reviewArtifactRef=_resource_ref(review_artifact),
        )

    async def _package_subject_artifacts(
        self,
        review: Review,
    ) -> tuple[tuple[PackagedReviewArtifact, ...], tuple[str, ...]]:
        artifacts: list[PackagedReviewArtifact] = []
        missing: list[str] = []
        for subject in review.spec.subject_refs:
            try:
                artifact = await self._artifact_repository.get(subject.id)
            except ResourceNotFoundError:
                missing.append(f"Artifact {subject.id} was not found")
                continue
            if artifact.metadata.resource_version != subject.resource_version:
                missing.append(
                    "Artifact "
                    f"{subject.id} resourceVersion changed from "
                    f"{subject.resource_version} to "
                    f"{artifact.metadata.resource_version}"
                )
                continue
            try:
                content = (await self._artifact_storage.read_bytes(artifact)).decode(
                    "utf-8",
                    errors="replace",
                )
            except ArtifactStorageError as error:
                missing.append(f"Artifact {subject.id} content unavailable: {error}")
                continue
            artifacts.append(
                PackagedReviewArtifact(
                    ref=subject,
                    type=artifact.spec.artifact_type,
                    mediaType=artifact.spec.media_type,
                    sha256=artifact.spec.sha256,
                    content=content,
                )
            )
        return tuple(artifacts), tuple(missing)

    async def _mark_running(self, review: Review) -> Review:
        if review.status.phase == ReviewPhase.RUNNING:
            return review
        if review.status.phase not in {ReviewPhase.PENDING, ReviewPhase.SCHEDULED}:
            raise ValueError(f"Review is {review.status.phase}, not schedulable")
        status = ReviewStatus(
            observedGeneration=review.metadata.generation,
            phase=ReviewPhase.RUNNING,
        )
        return await self._review_repository.update_status(
            review.metadata.id,
            status,
            expected_resource_version=review.metadata.resource_version,
        )

    async def _complete_unable_to_review(
        self,
        review: Review,
        missing_evidence: tuple[str, ...],
    ) -> ReviewerInvocationResult:
        output = ReviewerOutput(
            verdict=ReviewVerdict.UNABLE_TO_REVIEW,
            summary="Review could not run because required evidence is missing.",
            missingEvidence=missing_evidence,
        )
        status = _review_status_from_output(output)
        review_artifact = await self._create_artifact(
            review,
            name=f"review-output-{review.metadata.id.hex[:12]}",
            artifact_type=ArtifactType.REVIEW,
            media_type="application/json",
            content=_json_bytes(output.model_dump(mode="json", by_alias=True)),
            source_refs=_subject_resource_refs(review),
        )
        completed = await self._review_repository.update_status(
            review.metadata.id,
            _with_review_condition(review, status),
            expected_resource_version=review.metadata.resource_version,
        )
        await self._publish_review_event("ReviewCompleted", completed)
        return ReviewerInvocationResult(
            reviewRef=_resource_ref(completed),
            status=ReviewerInvocationStatus(verdict=completed.status.verdict),
            reviewArtifactRef=_resource_ref(review_artifact),
        )

    async def _mark_failed(self, review: Review, message: str) -> Review:
        current = await self._review_repository.get(review.metadata.id)
        status = ReviewStatus(
            observedGeneration=current.metadata.generation,
            phase=ReviewPhase.FAILED,
            failureMessage=message,
        )
        return await self._review_repository.update_status(
            current.metadata.id,
            status,
            expected_resource_version=current.metadata.resource_version,
        )

    async def _create_response_artifact(
        self,
        review: Review,
        response: StructuredGenerationResult,
        *,
        source_refs: tuple[ResourceReference, ...],
    ) -> Artifact:
        return await self._create_artifact(
            review,
            name=f"review-response-{review.metadata.id.hex[:12]}",
            artifact_type=ArtifactType.MODEL_RESPONSE,
            media_type="application/json",
            content=_json_bytes(
                {
                    "model": response.model,
                    "output": response.output,
                    "rawText": response.raw_text,
                    "tokenUsage": response.token_usage.model_dump(
                        mode="json",
                        by_alias=True,
                    ),
                }
            ),
            source_refs=source_refs,
        )

    async def _create_artifact(
        self,
        review: Review,
        *,
        name: ResourceName,
        artifact_type: ArtifactType,
        media_type: str,
        content: bytes,
        source_refs: tuple[ResourceReference, ...] = (),
    ) -> Artifact:
        artifact = await self._artifact_service.create_bytes_artifact(
            name=name,
            execution_ref=ArtifactExecutionReference(
                id=review.spec.execution_ref.id,
                name=review.spec.execution_ref.name,
            ),
            work_item_ref=ArtifactWorkItemReference(
                id=review.spec.work_item_ref.id,
                name=review.spec.work_item_ref.name,
            ),
            artifact_type=artifact_type,
            media_type=media_type,
            content=content,
            producer=ArtifactProducer(subsystem=REVIEWER_RUNTIME),
            source_refs=_unique_refs(source_refs),
        )
        return await self._artifact_service.verify_artifact(
            artifact,
            expected_resource_version=artifact.metadata.resource_version,
        )

    async def _publish_review_event(self, event_type: str, review: Review) -> None:
        if self._event_publisher is None:
            return
        await self._event_publisher.publish(
            EventDraft(
                type=event_type,
                producer=REVIEWER_RUNTIME,
                correlationId=f"review:{review.metadata.id}:{event_type}",
                executionRef=EventExecutionReference(
                    id=review.spec.execution_ref.id,
                    name=review.spec.execution_ref.name,
                ),
                subjectRef=_resource_ref(review),
                payload=_review_event_payload(review),
            )
        )


def build_reviewer_input(
    review: Review,
    subject_artifacts: tuple[PackagedReviewArtifact, ...],
) -> ReviewerInput:
    """Build provider-independent Reviewer input."""

    return ReviewerInput(
        reviewRef=_resource_ref(review),
        workItemRef=ResourceReference(
            kind=review.spec.work_item_ref.kind,
            id=review.spec.work_item_ref.id,
            name=review.spec.work_item_ref.name,
        ),
        acceptanceCriteria=review.spec.acceptance_criteria,
        policy=review.spec.policy.model_dump(mode="json", by_alias=True),
        capabilities={
            "granted": ("artifact.read",),
            "denied": REVIEWER_DENIED_CAPABILITIES,
        },
        subjectArtifacts=subject_artifacts,
    )


def _review_prompt(reviewer_input: ReviewerInput) -> str:
    prompt = {
        "instructions": REVIEWER_PROMPT_TEMPLATE,
        "input": reviewer_input.model_dump(mode="json", by_alias=True),
        "outputSchema": ReviewerOutput.model_json_schema(by_alias=True),
    }
    return _json_text(prompt)


def _review_status_from_output(output: ReviewerOutput) -> ReviewStatus:
    return ReviewStatus(
        observedGeneration=1,
        phase=ReviewPhase.COMPLETED,
        verdict=output.verdict,
        summary=output.summary,
        blockingFindings=output.blocking_findings,
        nonBlockingFindings=output.non_blocking_findings,
        missingEvidence=output.missing_evidence,
        completedAt=utc_now(),
    )


def _with_review_condition(review: Review, status: ReviewStatus) -> ReviewStatus:
    condition_status = (
        ConditionStatus.TRUE
        if status.verdict == ReviewVerdict.APPROVE
        else ConditionStatus.FALSE
    )
    return with_condition(
        review,
        observe_generation(review, status),
        condition_type="ReviewCompleted",
        condition_status=condition_status,
        reason=str(status.verdict or "Failed"),
        message=status.summary or status.failure_message,
    )


def _subject_resource_refs(review: Review) -> tuple[ResourceReference, ...]:
    return tuple(
        ResourceReference(kind=subject.kind, id=subject.id, name=subject.name)
        for subject in review.spec.subject_refs
    )


def _resource_ref(resource: Review | Artifact) -> ResourceReference:
    return ResourceReference(
        kind=resource.kind,
        id=resource.metadata.id,
        name=resource.metadata.name,
    )


def _unique_refs(refs: tuple[ResourceReference, ...]) -> tuple[ResourceReference, ...]:
    by_key = {(ref.kind, ref.id): ref for ref in refs}
    return tuple(by_key.values())


def _review_event_payload(review: Review) -> EventPayload:
    payload = {
        "phase": review.status.phase,
        "verdict": review.status.verdict,
        "summary": review.status.summary,
        "failureMessage": review.status.failure_message,
    }
    return cast(EventPayload, json.loads(json.dumps(payload)))


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _json_bytes(value: Any) -> bytes:
    return _json_text(value).encode("utf-8")

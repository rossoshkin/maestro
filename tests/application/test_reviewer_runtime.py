"""Tests for Reviewer Role runtime."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from maestro.application.artifacts import ArtifactService
from maestro.application.reviewer import (
    ReviewerRuntime,
    build_reviewer_input,
)
from maestro.domain.artifacts import (
    Artifact,
    ArtifactExecutionReference,
    ArtifactPhase,
    ArtifactProducer,
    ArtifactType,
    ArtifactWorkItemReference,
)
from maestro.domain.events import EventDraft
from maestro.domain.providers import (
    Provider,
    ProviderErrorCode,
    ProviderFailure,
    ProviderFeatureSet,
    ProviderHealth,
    ProviderModelList,
    ProviderOperationError,
    ProviderPhase,
    ProviderSpec,
    ProviderTokenUsage,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    ToolLoopRequest,
    ToolLoopResult,
)
from maestro.domain.resources import ConditionStatus
from maestro.domain.reviews import (
    Review,
    ReviewArtifactReference,
    ReviewExecutionReference,
    ReviewPhase,
    ReviewRoleReference,
    ReviewSpec,
    ReviewVerdict,
    ReviewWorkItemReference,
)
from maestro.infrastructure.artifacts import LocalArtifactStorage
from maestro.infrastructure.persistence import (
    SQLiteArtifactRepository,
    SQLiteReviewRepository,
)


class RecordingReviewerProvider:
    """Capture structured-generation requests and return queued outputs."""

    def __init__(
        self,
        outputs: Iterable[dict[str, Any]] = (),
        *,
        failure: ProviderFailure | None = None,
    ) -> None:
        self.calls: list[StructuredGenerationRequest] = []
        self._outputs = deque(outputs)
        self._failure = failure

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            phase=ProviderPhase.READY,
            capabilities=ProviderFeatureSet(structuredOutput=True),
            availableModels=("codex-reviewer",),
        )

    async def list_models(self) -> ProviderModelList:
        return ProviderModelList(models=("codex-reviewer",))

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
    ) -> StructuredGenerationResult:
        self.calls.append(request)
        if self._failure is not None:
            raise ProviderOperationError(self._failure)
        output = self._outputs.popleft() if self._outputs else approve_output()
        return StructuredGenerationResult(
            model=request.model,
            output=output,
            rawText=json.dumps(output),
            tokenUsage=ProviderTokenUsage(inputTokens=1, outputTokens=1),
        )

    async def run_tool_loop(self, request: ToolLoopRequest) -> ToolLoopResult:
        raise AssertionError("Reviewer runtime must not use tool loops")


class RecordingPublisher:
    """Capture review events."""

    def __init__(self) -> None:
        self.events: list[EventDraft] = []

    async def publish(self, draft: EventDraft) -> object:
        self.events.append(draft)
        return object()


@dataclass(slots=True)
class ReviewerHarness:
    """Repositories, runtime and resources for Reviewer runtime tests."""

    runtime: ReviewerRuntime
    reviews: SQLiteReviewRepository
    artifacts: SQLiteArtifactRepository
    artifact_storage: LocalArtifactStorage
    publisher: RecordingPublisher
    review: Review
    subject_artifacts: tuple[Artifact, ...]

    async def artifacts_for_execution(self) -> tuple[Artifact, ...]:
        return await self.artifacts.list_by_execution(self.review.spec.execution_ref.id)

    async def artifact_payload(self, artifact: Artifact) -> dict[str, Any]:
        return json.loads((await self.artifact_storage.read_bytes(artifact)).decode())

    def close(self) -> None:
        self.reviews.close()
        self.artifacts.close()


def approve_output() -> dict[str, Any]:
    return {
        "verdict": "Approve",
        "summary": "Implementation satisfies the recorded evidence.",
        "blockingFindings": (),
        "nonBlockingFindings": (),
        "missingEvidence": (),
    }


def request_changes_output() -> dict[str, Any]:
    return {
        "verdict": "RequestChanges",
        "summary": "Health response is still incorrect.",
        "blockingFindings": (
            {
                "id": "finding-1",
                "severity": "high",
                "category": "correctness",
                "file": "app.py",
                "line": 12,
                "issue": "Response body does not match acceptance criteria.",
                "evidence": "Diff returns {'ok': true}.",
                "suggestedFix": "Return {'status': 'ok'}.",
            },
        ),
        "nonBlockingFindings": (),
        "missingEvidence": (),
    }


def test_reviewer_runtime_records_structured_approve_review(tmp_path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        provider = RecordingReviewerProvider((approve_output(),))

        result = await harness.runtime.invoke_review(
            harness.review.metadata.id,
            provider=provider_resource(),
            runtime=provider,
            model="codex-reviewer",
        )

        updated = await harness.reviews.get(harness.review.metadata.id)
        artifacts = await harness.artifacts_for_execution()
        prompt_artifact = await harness.artifacts.get(result.prompt_artifact_ref.id)
        prompt = (await harness.artifact_storage.read_bytes(prompt_artifact)).decode()

        assert result.status.verdict == ReviewVerdict.APPROVE
        assert updated.status.phase == ReviewPhase.COMPLETED
        assert updated.status.verdict == ReviewVerdict.APPROVE
        assert updated.status.blocking_findings == ()
        assert updated.status.conditions[0].status is ConditionStatus.TRUE
        assert provider.calls[0].response_schema["properties"]["verdict"]
        assert '"granted": [\n        "artifact.read"\n      ]' in prompt
        assert '"allowWorkspaceMutation": false' in prompt
        assert {artifact.spec.artifact_type for artifact in artifacts} >= {
            ArtifactType.PROMPT,
            ArtifactType.MODEL_RESPONSE,
            ArtifactType.REVIEW,
        }
        assert all(
            artifact.status.phase == ArtifactPhase.AVAILABLE for artifact in artifacts
        )
        assert {event.event_type for event in harness.publisher.events} == {
            "ReviewCompleted"
        }
        harness.close()

    asyncio.run(scenario())


def test_reviewer_runtime_records_request_changes_review(tmp_path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        provider = RecordingReviewerProvider((request_changes_output(),))

        result = await harness.runtime.invoke_review(
            harness.review.metadata.id,
            provider=provider_resource(),
            runtime=provider,
            model="codex-reviewer",
        )

        updated = await harness.reviews.get(harness.review.metadata.id)

        assert result.status.verdict == ReviewVerdict.REQUEST_CHANGES
        assert updated.status.verdict == ReviewVerdict.REQUEST_CHANGES
        assert updated.status.blocking_findings[0].id == "finding-1"
        assert updated.status.conditions[0].status is ConditionStatus.FALSE
        harness.close()

    asyncio.run(scenario())


def test_reviewer_runtime_rejects_malformed_structured_output(tmp_path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        provider = RecordingReviewerProvider(({"verdict": "Approve"},))

        result = await harness.runtime.invoke_review(
            harness.review.metadata.id,
            provider=provider_resource(),
            runtime=provider,
            model="codex-reviewer",
        )

        updated = await harness.reviews.get(harness.review.metadata.id)
        artifacts = await harness.artifacts_for_execution()

        assert result.status.failed is True
        assert updated.status.phase == ReviewPhase.FAILED
        assert "ReviewOutputInvalid" in updated.status.failure_message
        assert {artifact.spec.artifact_type for artifact in artifacts} >= {
            ArtifactType.PROMPT,
            ArtifactType.MODEL_RESPONSE,
        }
        assert ArtifactType.REVIEW not in {
            artifact.spec.artifact_type
            for artifact in artifacts
            if artifact.metadata.name.startswith("review-output")
        }
        harness.close()

    asyncio.run(scenario())


def test_reviewer_runtime_normalizes_provider_timeout(tmp_path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        provider = RecordingReviewerProvider(
            failure=ProviderFailure(
                code=ProviderErrorCode.PROVIDER_TIMEOUT,
                message="Codex timed out",
                retryable=True,
            )
        )

        result = await harness.runtime.invoke_review(
            harness.review.metadata.id,
            provider=provider_resource(timeout_seconds=1),
            runtime=provider,
            model="codex-reviewer",
        )

        updated = await harness.reviews.get(harness.review.metadata.id)

        assert result.status.failed is True
        assert updated.status.phase == ReviewPhase.FAILED
        assert "ProviderTimeout" in updated.status.failure_message
        assert provider.calls[0].timeout_seconds == 1
        harness.close()

    asyncio.run(scenario())


def test_reviewer_runtime_returns_unable_to_review_for_missing_artifact(
    tmp_path,
) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path, missing_subject=True)
        provider = RecordingReviewerProvider((approve_output(),))

        result = await harness.runtime.invoke_review(
            harness.review.metadata.id,
            provider=provider_resource(),
            runtime=provider,
            model="codex-reviewer",
        )

        updated = await harness.reviews.get(harness.review.metadata.id)
        review_artifact = await harness.artifacts.get(result.review_artifact_ref.id)
        payload = await harness.artifact_payload(review_artifact)

        assert result.status.verdict == ReviewVerdict.UNABLE_TO_REVIEW
        assert updated.status.phase == ReviewPhase.COMPLETED
        assert updated.status.verdict == ReviewVerdict.UNABLE_TO_REVIEW
        assert updated.status.missing_evidence
        assert provider.calls == []
        assert payload["verdict"] == "UnableToReview"
        harness.close()

    asyncio.run(scenario())


def test_build_reviewer_input_uses_read_only_capabilities(tmp_path) -> None:
    async def scenario() -> None:
        harness = await make_harness(tmp_path)
        packaged, missing = await harness.runtime._package_subject_artifacts(  # noqa: SLF001
            harness.review
        )

        reviewer_input = build_reviewer_input(harness.review, packaged)

        assert missing == ()
        assert reviewer_input.capabilities["granted"] == ("artifact.read",)
        assert "filesystem.write" in reviewer_input.capabilities["denied"]
        assert reviewer_input.policy["allowWorkspaceMutation"] is False
        harness.close()

    asyncio.run(scenario())


async def make_harness(
    tmp_path,
    *,
    missing_subject: bool = False,
) -> ReviewerHarness:
    reviews = SQLiteReviewRepository(":memory:")
    artifacts = SQLiteArtifactRepository(":memory:")
    artifact_storage = LocalArtifactStorage(tmp_path / "artifacts")
    artifact_service = ArtifactService(artifacts, artifact_storage)
    publisher = RecordingPublisher()
    runtime = ReviewerRuntime(
        review_repository=reviews,
        artifact_repository=artifacts,
        artifact_storage=artifact_storage,
        artifact_service=artifact_service,
        event_publisher=publisher,
    )
    execution_id = uuid4()
    work_item_id = uuid4()
    subject_artifacts = await create_subject_artifacts(
        artifact_service,
        execution_id=execution_id,
        work_item_id=work_item_id,
    )
    subject_refs = tuple(
        ReviewArtifactReference(
            id=artifact.metadata.id,
            name=artifact.metadata.name,
            resourceVersion=artifact.metadata.resource_version,
        )
        for artifact in subject_artifacts
    )
    if missing_subject:
        subject_refs = (
            ReviewArtifactReference(
                id=uuid4(),
                name="missing-artifact",
                resourceVersion=1,
            ),
        )
    review = await reviews.create(
        Review.new(
            name="review-1",
            spec=ReviewSpec(
                executionRef=ReviewExecutionReference(
                    id=execution_id,
                    name="implement-health",
                ),
                workItemRef=ReviewWorkItemReference(
                    id=work_item_id,
                    name="add-health",
                ),
                reviewerRoleRef=ReviewRoleReference(
                    name="reviewer",
                    version="v1alpha1",
                ),
                subjectRefs=subject_refs,
                acceptanceCriteria=("GET /health returns 200",),
            ),
        )
    )
    return ReviewerHarness(
        runtime=runtime,
        reviews=reviews,
        artifacts=artifacts,
        artifact_storage=artifact_storage,
        publisher=publisher,
        review=review,
        subject_artifacts=subject_artifacts,
    )


async def create_subject_artifacts(
    service: ArtifactService,
    *,
    execution_id: UUID,
    work_item_id: UUID,
) -> tuple[Artifact, ...]:
    execution_ref = ArtifactExecutionReference(
        id=execution_id,
        name="implement-health",
    )
    work_item_ref = ArtifactWorkItemReference(id=work_item_id, name="add-health")
    created: list[Artifact] = []
    for name, artifact_type, content in (
        (
            "coding-summary",
            ArtifactType.SUMMARY,
            b'{"summary": "Implemented health endpoint"}',
        ),
        (
            "git-diff",
            ArtifactType.GIT_DIFF,
            b"diff --git a/app.py b/app.py\n+return {'status': 'ok'}\n",
        ),
        (
            "verification-report",
            ArtifactType.VERIFICATION_REPORT,
            b'{"status": "passed", "allCommandsPassed": true}',
        ),
    ):
        artifact = await service.create_bytes_artifact(
            name=name,
            execution_ref=execution_ref,
            work_item_ref=work_item_ref,
            artifact_type=artifact_type,
            media_type="application/json"
            if artifact_type != ArtifactType.GIT_DIFF
            else "text/x-diff",
            content=content,
            producer=ArtifactProducer(subsystem="test"),
        )
        created.append(
            await service.verify_artifact(
                artifact,
                expected_resource_version=artifact.metadata.resource_version,
            )
        )
    return tuple(created)


def provider_resource(*, timeout_seconds: int = 30) -> Provider:
    return Provider.new(
        name="codex-local",
        spec=ProviderSpec(
            type="codex",
            endpoint="codex",
            allowedModels=("codex-reviewer",),
            timeoutSeconds=timeout_seconds,
        ),
    )

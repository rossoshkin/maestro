"""Tests for Review resources."""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.exceptions import ResourceImmutableFieldError
from maestro.domain.resources import Metadata, OwnerReference, utc_now
from maestro.domain.reviews import (
    Review,
    ReviewArtifactReference,
    ReviewExecutionReference,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingSeverity,
    ReviewPhase,
    ReviewPolicy,
    ReviewRoleReference,
    ReviewSpec,
    ReviewStatus,
    ReviewVerdict,
    ReviewWorkItemReference,
    apply_review_spec_update,
)


def valid_review_spec(
    execution_id: UUID | None = None,
    work_item_id: UUID | None = None,
    artifact_id: UUID | None = None,
) -> ReviewSpec:
    """Build a valid ReviewSpec for tests."""

    return ReviewSpec(
        executionRef=ReviewExecutionReference(
            id=execution_id or uuid4(),
            name="implement-health",
        ),
        workItemRef=ReviewWorkItemReference(
            id=work_item_id or uuid4(),
            name="add-health",
        ),
        reviewerRoleRef=ReviewRoleReference(name="reviewer", version="v1alpha1"),
        subjectRefs=(
            ReviewArtifactReference(
                id=artifact_id or uuid4(),
                name="git-diff",
                resourceVersion=2,
            ),
        ),
        acceptanceCriteria=("GET /health returns 200",),
    )


def valid_review() -> Review:
    """Build a valid Review resource."""

    return Review.new(name="review-1", spec=valid_review_spec())


def blocking_finding() -> ReviewFinding:
    """Build a blocking review finding."""

    return ReviewFinding(
        id="finding-1",
        severity=ReviewFindingSeverity.HIGH,
        category=ReviewFindingCategory.CORRECTNESS,
        file="app/main.py",
        line=14,
        issue="Response body does not match acceptance criteria",
        evidence='Current response is {"ok": true}',
        suggestedFix='Return {"status": "ok"}',
    )


def test_review_serializes_and_deserializes() -> None:
    review = valid_review()

    payload = review.model_dump(mode="json", by_alias=True)
    round_tripped = Review.model_validate(payload)

    assert payload["kind"] == "Review"
    assert payload["spec"]["subjectRefs"][0]["kind"] == "Artifact"
    assert payload["spec"]["policy"]["allowWorkspaceMutation"] is False
    assert round_tripped == review


def test_review_requires_matching_execution_owner() -> None:
    spec = valid_review_spec()

    with pytest.raises(ValidationError):
        Review(
            metadata=Metadata(
                name="review-1",
                ownerReferences=(
                    OwnerReference(
                        kind="Execution",
                        id=uuid4(),
                        controller=True,
                    ),
                ),
            ),
            spec=spec,
            status=ReviewStatus(),
        )


def test_review_subjects_must_be_artifacts() -> None:
    payload = valid_review_spec().model_dump(mode="python", by_alias=True)
    payload["subjectRefs"][0]["kind"] = "WorkItem"

    with pytest.raises(ValidationError):
        ReviewSpec.model_validate(payload)


def test_reviewer_policy_is_read_only_for_workspace() -> None:
    with pytest.raises(ValidationError):
        ReviewPolicy(allowWorkspaceMutation=True)


def test_review_approve_verdict_rejects_blocking_findings() -> None:
    with pytest.raises(ValidationError):
        ReviewStatus(
            phase=ReviewPhase.COMPLETED,
            verdict=ReviewVerdict.APPROVE,
            blockingFindings=(blocking_finding(),),
            completedAt=utc_now(),
        )


def test_review_request_changes_requires_blocking_findings() -> None:
    with pytest.raises(ValidationError):
        ReviewStatus(
            phase=ReviewPhase.COMPLETED,
            verdict=ReviewVerdict.REQUEST_CHANGES,
            completedAt=utc_now(),
        )

    status = ReviewStatus(
        phase=ReviewPhase.COMPLETED,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        blockingFindings=(blocking_finding(),),
        completedAt=utc_now(),
    )

    assert status.verdict == ReviewVerdict.REQUEST_CHANGES
    assert len(status.blocking_findings) == 1


def test_review_unable_to_review_requires_missing_evidence() -> None:
    with pytest.raises(ValidationError):
        ReviewStatus(
            phase=ReviewPhase.COMPLETED,
            verdict=ReviewVerdict.UNABLE_TO_REVIEW,
            completedAt=utc_now(),
        )

    status = ReviewStatus(
        phase=ReviewPhase.COMPLETED,
        verdict=ReviewVerdict.UNABLE_TO_REVIEW,
        missingEvidence=("verification report artifact is missing",),
        completedAt=utc_now(),
    )

    assert status.missing_evidence == ("verification report artifact is missing",)


def test_blocking_and_non_blocking_findings_remain_distinct() -> None:
    finding = blocking_finding()

    with pytest.raises(ValidationError):
        ReviewStatus(
            phase=ReviewPhase.COMPLETED,
            verdict=ReviewVerdict.REQUEST_CHANGES,
            blockingFindings=(finding,),
            nonBlockingFindings=(finding,),
            completedAt=utc_now(),
        )


def test_review_spec_updates_are_rejected() -> None:
    review = valid_review()
    changed_spec = review.spec.model_copy(update={"acceptance_criteria": ("Changed",)})

    with pytest.raises(ResourceImmutableFieldError):
        apply_review_spec_update(
            review,
            changed_spec,
            expected_resource_version=review.metadata.resource_version,
        )

"""Tests for SQLite Review persistence."""

import asyncio
from uuid import UUID, uuid4

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.exceptions import (
    ResourceConflictError,
    ResourceImmutableFieldError,
)
from maestro.domain.resources import utc_now
from maestro.domain.reviews import (
    Review,
    ReviewArtifactReference,
    ReviewExecutionReference,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingSeverity,
    ReviewPhase,
    ReviewRepository,
    ReviewRoleReference,
    ReviewSpec,
    ReviewStatus,
    ReviewVerdict,
    ReviewWorkItemReference,
)
from maestro.infrastructure.persistence import SQLiteReviewRepository


def valid_review_spec(
    execution_id: UUID | None = None,
    *,
    work_item_id: UUID | None = None,
) -> ReviewSpec:
    """Build a valid ReviewSpec for persistence tests."""

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
                id=uuid4(),
                name="git-diff",
                resourceVersion=2,
            ),
            ReviewArtifactReference(
                id=uuid4(),
                name="verification-report",
                resourceVersion=1,
            ),
        ),
        acceptanceCriteria=("GET /health returns 200",),
    )


def valid_review(
    execution_id: UUID | None = None,
    *,
    work_item_id: UUID | None = None,
    name: str = "review-1",
) -> Review:
    """Build a valid Review resource."""

    return Review.new(
        name=name,
        spec=valid_review_spec(execution_id, work_item_id=work_item_id),
    )


def finding(finding_id: str = "finding-1") -> ReviewFinding:
    """Build a review finding."""

    return ReviewFinding(
        id=finding_id,
        severity=ReviewFindingSeverity.HIGH,
        category=ReviewFindingCategory.CORRECTNESS,
        file="app/main.py",
        line=14,
        issue="Response body does not match acceptance criteria",
        evidence='Current response is {"ok": true}',
        suggestedFix='Return {"status": "ok"}',
    )


def request_changes_status() -> ReviewStatus:
    """Build a completed RequestChanges status."""

    return ReviewStatus(
        observedGeneration=1,
        phase=ReviewPhase.COMPLETED,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        blockingFindings=(finding("blocking-1"),),
        nonBlockingFindings=(finding("nonblocking-1"),),
        completedAt=utc_now(),
    )


def test_review_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteReviewRepository(tmp_path / "maestro.db")
        review = await repository.create(valid_review())
        loaded = await repository.get(review.metadata.id)

        assert loaded == review
        repository.close()

    asyncio.run(scenario())


def test_review_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteReviewRepository(database_path)
        review = await first_repository.create(valid_review())
        first_repository.close()

        second_repository = SQLiteReviewRepository(database_path)
        loaded = await second_repository.get(review.metadata.id)

        assert loaded.metadata.id == review.metadata.id
        assert loaded.spec.reviewer_role_ref.name == "reviewer"
        second_repository.close()

    asyncio.run(scenario())


def test_review_repository_lists_by_execution_work_item_and_labels() -> None:
    async def scenario() -> None:
        repository = SQLiteReviewRepository(":memory:")
        execution_id = uuid4()
        work_item_id = uuid4()
        review = valid_review(execution_id, work_item_id=work_item_id)
        labeled_review = review.model_copy(
            update={
                "metadata": review.metadata.model_copy(
                    update={"labels": {"role": "reviewer"}}
                )
            }
        )
        await repository.create(labeled_review)
        await repository.create(valid_review(name="other-review"))

        by_execution = await repository.list_by_execution(execution_id)
        by_work_item = await repository.list_by_work_item(work_item_id)
        by_label = await repository.list(ResourceSelector(labels={"role": "reviewer"}))

        assert [review.metadata.name for review in by_execution] == ["review-1"]
        assert [review.metadata.name for review in by_work_item] == ["review-1"]
        assert [review.metadata.name for review in by_label] == ["review-1"]
        repository.close()

    asyncio.run(scenario())


def test_review_update_status_persists_findings_distinctly() -> None:
    async def scenario(repository: ReviewRepository) -> None:
        review = await repository.create(valid_review())

        updated = await repository.update_status(
            review.metadata.id,
            request_changes_status(),
            expected_resource_version=review.metadata.resource_version,
        )

        assert updated.status.phase == ReviewPhase.COMPLETED
        assert updated.status.verdict == ReviewVerdict.REQUEST_CHANGES
        assert [finding.id for finding in updated.status.blocking_findings] == [
            "blocking-1"
        ]
        assert [finding.id for finding in updated.status.non_blocking_findings] == [
            "nonblocking-1"
        ]

    repository = SQLiteReviewRepository(":memory:")
    asyncio.run(scenario(repository))
    repository.close()


def test_review_spec_updates_are_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteReviewRepository(":memory:")
        review = await repository.create(valid_review())
        changed_spec = review.spec.model_copy(
            update={"acceptance_criteria": ("Changed",)}
        )

        with pytest.raises(ResourceImmutableFieldError):
            await repository.update_spec(
                review.metadata.id,
                changed_spec,
                expected_resource_version=review.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_review_stale_status_update_returns_conflict() -> None:
    async def scenario() -> None:
        repository = SQLiteReviewRepository(":memory:")
        review = await repository.create(valid_review())
        status = request_changes_status()
        await repository.update_status(
            review.metadata.id,
            status,
            expected_resource_version=review.metadata.resource_version,
        )

        with pytest.raises(ResourceConflictError):
            await repository.update_status(
                review.metadata.id,
                status,
                expected_resource_version=review.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())

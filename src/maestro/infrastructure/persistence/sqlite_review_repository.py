"""SQLite persistence for Review resources."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from maestro.domain.repositories import ResourceSelector
from maestro.domain.reviews import (
    Review,
    ReviewRepository,
    ReviewSpec,
    ReviewStatus,
    apply_review_spec_update,
    apply_review_status_update,
)


class SQLiteReviewRepository(ReviewRepository):
    """SQLite-backed Review repository."""

    def __init__(self, database_path: Path | str) -> None:
        self._database_path = database_path
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(database_path),
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        """Close the SQLite connection."""

        self._connection.close()

    async def create(self, resource: Review) -> Review:
        """Persist a new Review."""

        try:
            self._connection.execute(
                """
                INSERT INTO reviews (
                    id,
                    execution_id,
                    work_item_id,
                    namespace,
                    name,
                    generation,
                    resource_version,
                    phase,
                    verdict,
                    resource_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row_values(resource),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            raise ResourceAlreadyExistsError(
                resource.kind,
                resource.metadata.namespace,
                resource.metadata.name,
            ) from error
        return resource

    async def get(self, resource_id: UUID) -> Review:
        """Load a Review by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM reviews WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Review.model_validate_json(row["resource_json"])

    async def list(
        self,
        selector: ResourceSelector | None = None,
    ) -> tuple[Review, ...]:
        """List Reviews matching optional selection criteria."""

        rows = self._connection.execute(
            "SELECT resource_json FROM reviews ORDER BY namespace, name"
        ).fetchall()
        reviews = tuple(
            Review.model_validate_json(row["resource_json"]) for row in rows
        )
        if selector is None:
            return reviews
        return tuple(review for review in reviews if self._matches(review, selector))

    async def list_by_execution(self, execution_id: UUID) -> tuple[Review, ...]:
        """List Reviews belonging to one Execution."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM reviews
            WHERE execution_id = ?
            ORDER BY namespace, name
            """,
            (str(execution_id),),
        ).fetchall()
        return tuple(Review.model_validate_json(row["resource_json"]) for row in rows)

    async def list_by_work_item(self, work_item_id: UUID) -> tuple[Review, ...]:
        """List Reviews for one WorkItem."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM reviews
            WHERE work_item_id = ?
            ORDER BY namespace, name
            """,
            (str(work_item_id),),
        ).fetchall()
        return tuple(Review.model_validate_json(row["resource_json"]) for row in rows)

    async def update_spec(
        self,
        resource_id: UUID,
        spec: ReviewSpec,
        *,
        expected_resource_version: int,
    ) -> Review:
        """Reject Review spec changes and persist no-op updates."""

        review = await self.get(resource_id)
        updated = apply_review_spec_update(
            review,
            spec,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    async def update_status(
        self,
        resource_id: UUID,
        status: ReviewStatus,
        *,
        expected_resource_version: int,
    ) -> Review:
        """Persist a Review status update."""

        review = await self.get(resource_id)
        updated = apply_review_status_update(
            review,
            status,
            expected_resource_version=expected_resource_version,
        )
        self._replace(updated, expected_resource_version=expected_resource_version)
        return updated

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                work_item_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                resource_version INTEGER NOT NULL,
                phase TEXT NOT NULL,
                verdict TEXT,
                resource_json TEXT NOT NULL,
                UNIQUE(namespace, name)
            )
            """
        )
        self._connection.commit()

    def _replace(
        self,
        review: Review,
        *,
        expected_resource_version: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE reviews
            SET resource_version = ?,
                phase = ?,
                verdict = ?,
                resource_json = ?
            WHERE id = ? AND resource_version = ?
            """,
            (
                review.metadata.resource_version,
                review.status.phase,
                review.status.verdict,
                self._serialize(review),
                str(review.metadata.id),
                expected_resource_version,
            ),
        )
        if cursor.rowcount != 1:
            current = self._connection.execute(
                "SELECT resource_json FROM reviews WHERE id = ?",
                (str(review.metadata.id),),
            ).fetchone()
            if current is None:
                raise ResourceNotFoundError(review.metadata.id)
            actual = Review.model_validate_json(current["resource_json"])
            raise ResourceConflictError(
                review.metadata.id,
                expected_resource_version,
                actual.metadata.resource_version,
            )
        self._connection.commit()

    def _row_values(
        self,
        review: Review,
    ) -> tuple[str, str, str, str, str, int, int, str, str | None, str]:
        return (
            str(review.metadata.id),
            str(review.spec.execution_ref.id),
            str(review.spec.work_item_ref.id),
            review.metadata.namespace,
            review.metadata.name,
            review.metadata.generation,
            review.metadata.resource_version,
            review.status.phase,
            review.status.verdict,
            self._serialize(review),
        )

    @staticmethod
    def _serialize(review: Review) -> str:
        return review.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(review: Review, selector: ResourceSelector) -> bool:
        namespace_matches = (
            selector.namespace is None
            or review.metadata.namespace == selector.namespace
        )
        labels_match = all(
            review.metadata.labels.get(key) == value
            for key, value in selector.labels.items()
        )
        return namespace_matches and labels_match

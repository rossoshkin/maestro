"""SQLite append-only Event store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from maestro.domain.events import (
    Event,
    EventDraft,
    EventQuery,
    EventSpec,
    EventStore,
    event_name,
    event_spec_matches_draft,
)
from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceNotFoundError,
)
from maestro.domain.resources import ResourceReference


class SQLiteEventStore(EventStore):
    """SQLite-backed append-only Event store."""

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

    async def publish(self, draft: EventDraft) -> Event:
        """Publish an Event draft."""

        return await self.append(draft)

    async def append(self, draft: EventDraft) -> Event:
        """Append an Event draft, assigning sequence within its Execution."""

        existing = self._find_duplicate(draft)
        if existing is not None:
            if event_spec_matches_draft(existing, draft):
                return existing
            raise ResourceAlreadyExistsError(
                "Event",
                "correlation",
                _duplicate_key(draft),
            )

        sequence = self._next_sequence(draft.execution_ref.id)
        event = Event.new(
            name=event_name(draft.execution_ref.id, sequence),
            spec=EventSpec.from_draft(draft, sequence=sequence),
        )

        try:
            self._connection.execute(
                """
                INSERT INTO events (
                    id,
                    execution_id,
                    namespace,
                    name,
                    sequence,
                    event_type,
                    correlation_id,
                    subject_kind,
                    subject_id,
                    occurred_at,
                    resource_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row_values(event),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            existing = self._find_duplicate(draft)
            if existing is not None and event_spec_matches_draft(existing, draft):
                return existing
            raise ResourceAlreadyExistsError(
                "Event",
                event.metadata.namespace,
                event.metadata.name,
            ) from error

        return event

    async def get(self, resource_id: UUID) -> Event:
        """Load an Event by ID."""

        row = self._connection.execute(
            "SELECT resource_json FROM events WHERE id = ?",
            (str(resource_id),),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return Event.model_validate_json(row["resource_json"])

    async def list(self, query: EventQuery | None = None) -> tuple[Event, ...]:
        """List Events matching optional filters."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM events
            ORDER BY execution_id, sequence
            """
        ).fetchall()
        events = tuple(Event.model_validate_json(row["resource_json"]) for row in rows)
        if query is None:
            return events
        return tuple(event for event in events if self._matches(event, query))

    async def list_by_execution(self, execution_id: UUID) -> tuple[Event, ...]:
        """List Events for one Execution ordered by sequence."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM events
            WHERE execution_id = ?
            ORDER BY sequence
            """,
            (str(execution_id),),
        ).fetchall()
        return tuple(Event.model_validate_json(row["resource_json"]) for row in rows)

    async def list_by_correlation(
        self,
        execution_id: UUID,
        correlation_id: str,
    ) -> tuple[Event, ...]:
        """List Events sharing one correlation ID within an Execution."""

        rows = self._connection.execute(
            """
            SELECT resource_json FROM events
            WHERE execution_id = ? AND correlation_id = ?
            ORDER BY sequence
            """,
            (str(execution_id), correlation_id),
        ).fetchall()
        return tuple(Event.model_validate_json(row["resource_json"]) for row in rows)

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                name TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                subject_kind TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                resource_json TEXT NOT NULL,
                UNIQUE(namespace, name),
                UNIQUE(execution_id, sequence),
                UNIQUE(
                    execution_id,
                    correlation_id,
                    event_type,
                    subject_kind,
                    subject_id
                )
            )
            """
        )
        self._connection.commit()

    def _next_sequence(self, execution_id: UUID) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM events
            WHERE execution_id = ?
            """,
            (str(execution_id),),
        ).fetchone()
        return int(row["next_sequence"])

    def _find_duplicate(self, draft: EventDraft) -> Event | None:
        row = self._connection.execute(
            """
            SELECT resource_json FROM events
            WHERE execution_id = ?
              AND correlation_id = ?
              AND event_type = ?
              AND subject_kind = ?
              AND subject_id = ?
            """,
            (
                str(draft.execution_ref.id),
                draft.correlation_id,
                draft.event_type,
                draft.subject_ref.kind,
                str(draft.subject_ref.id),
            ),
        ).fetchone()
        if row is None:
            return None
        return Event.model_validate_json(row["resource_json"])

    def _row_values(
        self,
        event: Event,
    ) -> tuple[str, str, str, str, int, str, str, str, str, str, str]:
        return (
            str(event.metadata.id),
            str(event.spec.execution_ref.id),
            event.metadata.namespace,
            event.metadata.name,
            event.spec.sequence,
            event.spec.event_type,
            event.spec.correlation_id,
            event.spec.subject_ref.kind,
            str(event.spec.subject_ref.id),
            event.spec.occurred_at.isoformat(),
            self._serialize(event),
        )

    @staticmethod
    def _serialize(event: Event) -> str:
        return event.model_dump_json(by_alias=True)

    @staticmethod
    def _matches(event: Event, query: EventQuery) -> bool:
        return (
            (
                query.execution_id is None
                or event.spec.execution_ref.id == query.execution_id
            )
            and (query.event_type is None or event.spec.event_type == query.event_type)
            and (
                query.correlation_id is None
                or event.spec.correlation_id == query.correlation_id
            )
            and (
                query.subject_ref is None
                or _same_subject(event.spec.subject_ref, query.subject_ref)
            )
        )


def _same_subject(left: ResourceReference, right: ResourceReference) -> bool:
    return left.kind == right.kind and left.id == right.id


def _duplicate_key(draft: EventDraft) -> str:
    return (
        f"{draft.execution_ref.id}/{draft.correlation_id}/"
        f"{draft.event_type}/{draft.subject_ref.kind}/{draft.subject_ref.id}"
    )

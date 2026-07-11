# Maestro MVP Implementation Plan

Status: Living document

Current milestone: Milestone 2 — Project

## Milestone 0 — Repository Bootstrap

Status: Complete on 2026-07-11.

Goal:
Create a production-ready project skeleton.

Deliverables:
- uv project
- pyproject.toml
- src/ layout
- tests/ layout
- FastAPI app
- Typer CLI
- Ruff
- mypy
- pytest
- pre-commit
- configuration
- structured logging

Acceptance Criteria:
- uv sync succeeds
- pytest passes
- ruff check passes
- mypy passes
- uv run uvicorn starts the API
- uv run maestro --help works

Exit Criteria:
Repository ready for domain implementation.

Completion Notes:
- Project metadata and dependency management are defined in `pyproject.toml`.
- Runtime code uses a `src/` layout under the `maestro` package.
- Package boundaries are prepared for domain, application, infrastructure and presentation layers.
- FastAPI exposes `/health/live` and `/health/ready`.
- Typer exposes the `maestro` CLI.
- Configuration uses documented `MAESTRO_` environment variables.
- Logging emits structured JSON records.
- Verification completed:
  - `uv sync`
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run mypy src`
  - `uv run pre-commit run --all-files`
  - `uv run maestro --help`
  - `uv run uvicorn maestro.presentation.api:app --host 127.0.0.1 --port 8765`

## Milestone 1 — Core Resource Framework

Status: Complete on 2026-07-11.

Deliverables:
- BaseResource
- Metadata
- Spec
- Status
- Condition
- Repository interfaces
Acceptance:
- serialization tests
- validation tests
- optimistic concurrency

Completion Notes:
- Added provider-independent resource primitives in `maestro.domain.resources`.
- Added typed domain exceptions in `maestro.domain.exceptions`.
- Added repository contracts and revision helpers in `maestro.domain.repositories`.
- Implemented validation for canonical API version, resource names, metadata, conditions, observed generation, finalizer uniqueness and secret-like metadata.
- Implemented optimistic concurrency helpers for spec and status updates.
- Preserved generation semantics: spec changes increment `generation`; status changes do not.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`
  - `uv run pre-commit run --all-files`

## Milestone 2 — Project
## Milestone 3 — Execution
## Milestone 4 — Workflow & Plan
## Milestone 5 — WorkItem / Role / Agent
## Milestone 6 — Workspace / Provider / Capability
## Milestone 7 — Controllers
## Milestone 8 — Planner
## Milestone 9 — Coding
## Milestone 10 — Verification & Review
## Milestone 11 — REST API
## Milestone 12 — Web UI
## Milestone 13 — End-to-End MVP

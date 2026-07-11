# Maestro MVP Implementation Plan

Status: Living document

## Milestone 0 — Repository Bootstrap

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

## Milestone 1 — Core Resource Framework
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

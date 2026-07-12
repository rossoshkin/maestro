# MVP Release Notes

Target release: `v0.1.0` local MVP

Date: 2026-07-12

## Highlights

- Local-first orchestration resources persisted in SQLite.
- Execution-centered workflow with Plan approval, Workspace preparation,
  Coding, independent verification, Review, repair, and final approval.
- Immutable Events and Artifacts for prompts, responses, plans, tool logs,
  diffs, verification reports, and reviews.
- Provider abstraction with Ollama and Codex adapters.
- Safe Coding tool runtime constrained to the prepared Workspace.
- Local Git worktree provider for isolated changes.
- REST API and browser UI for inspecting and acting on MVP resources.
- End-to-end MVP harness with restart recovery and source-checkout protection.

## Validated MVP Scenario

The automated demo creates a minimal FastAPI health endpoint, adds an automated
test, updates README instructions, verifies with pytest, routes one reviewer
repair loop, and completes after final human approval.

Run it with:

```bash
uv run pytest tests/e2e/test_mvp_vertical_slice.py
```

## Known Limits

- No automatic Git push or merge.
- No remote workers or concurrent Execution orchestration.
- No authentication.
- No cloud or Kubernetes deployment.
- Browser UI is an MVP operational console, not a full dashboard.

## Release Verification

Before tagging `v0.1.0`, run:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pre-commit run --all-files
```


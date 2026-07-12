# Maestro MVP Demo

This demo validates the local-first MVP workflow without requiring live model
calls. It uses deterministic provider doubles for repeatability and the real
SQLite repositories, Artifact storage, resource controllers, Coding tools,
local Git worktree provider, verification controller, and reviewer packaging.

## Prerequisites

```bash
uv sync
export OLLAMA_HOST=http://127.0.0.1:11434
ollama list
codex --version
```

Ollama and Codex are required for real local provider runs. The automated MVP
harness below uses scripted providers so CI and local development remain stable.

## Run the Automated MVP Scenario

```bash
uv run pytest tests/e2e/test_mvp_vertical_slice.py
```

The harness creates a fixture Git repository from
`tests/fixtures/fastapi_health_app`, then drives this goal:

```text
Create a minimal FastAPI application.

Requirements:
- GET /health returns {"status":"ok"}
- Add one automated test
- Add README instructions
- Do not add a database
- Do not add authentication
```

## What the Harness Proves

- Human Goal creation is represented by a persisted Execution and Event.
- Planner output creates a valid Plan and immutable Plan artifacts.
- Human Plan approval gates WorkItem materialization.
- Workspace preparation uses an isolated local Git worktree.
- Coding tools write only inside the Workspace; the source checkout stays clean.
- Verification runs independently with `python -m pytest -q`.
- Reviewer runs over immutable Artifact versions.
- A `RequestChanges` verdict creates a repair WorkItem.
- The repaired result is verified, reviewed, approved, and completed.
- Application restart is simulated by closing and reopening all SQLite adapters
  while the Execution is active.

## Failure Scenario Coverage

| Scenario | Coverage |
|---|---|
| Ollama unavailable | `tests/infrastructure/test_ollama_provider.py` |
| Codex unavailable | `tests/infrastructure/test_codex_provider.py` and reviewer provider-failure tests |
| invalid Planner output | `tests/application/test_planner_runtime.py` |
| Coding tool timeout | `tests/application/test_coding_runtime.py` |
| path traversal attempt | `tests/application/test_coding_runtime.py` and `tests/application/test_coding_tools.py` |
| failed verification | `tests/application/test_verification_controller.py` |
| Reviewer requests changes | `tests/e2e/test_mvp_vertical_slice.py` |
| repair limit exceeded | `tests/application/test_resource_controllers.py` |
| application restart while active | `tests/e2e/test_mvp_vertical_slice.py` |
| stale resourceVersion update | `tests/test_api.py` |

## Browser Smoke Demo

Start Maestro:

```bash
OLLAMA_HOST=http://127.0.0.1:11434 uv run maestro serve
```

Open:

```text
http://127.0.0.1:7860/ui
```

Use the UI to inspect Projects, Executions, Plans, Work Items, Role Invocations,
Artifacts, Reviews, Approvals, and Events. The UI is intentionally operational:
it does not own orchestration logic.


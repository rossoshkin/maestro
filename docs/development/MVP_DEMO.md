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

Use the Project form in the left sidebar to create a Project, then use the New
Execution form in the main panel to create an Execution. Select the Execution
and click `Run`; a draft Execution starts planning, and an already-started
Execution resumes the local backend runner. The UI inspects Plans, Work Items,
Role Invocations, Artifacts, Reviews, Approvals, and Events while the backend
runner invokes Ollama/Codex, prepares Workspaces, codes, verifies, reviews, and
stops for human approvals.

## Manual Browser Walkthrough

The UI is the human control-plane console for the MVP. It can create Projects
and Executions, start or resume the backend runner, show resource state, inspect
evidence, and record approval decisions. The canonical executable regression is
`tests/e2e/test_mvp_vertical_slice.py`.

Use this walkthrough to reproduce the MVP flow from the browser.

### 1. Start Local Prerequisites

```bash
export OLLAMA_HOST=http://127.0.0.1:11434
ollama list
codex --version
uv sync
```

### 2. Start Maestro With Stable Demo Data

Use explicit data paths so restart recovery is easy to inspect:

```bash
MAESTRO_DATABASE_URL=sqlite:///./data/mvp-demo.db \
MAESTRO_ARTIFACT_ROOT=./data/mvp-demo-artifacts \
MAESTRO_WORKSPACE_ROOT=./data/mvp-demo-workspaces \
OLLAMA_HOST=http://127.0.0.1:11434 \
uv run maestro serve
```

Then open:

```text
http://127.0.0.1:7860/ui
```

### 3. Prepare a Demo Project

Initialize a temporary Git repository from the automated fixture:

```text
tests/fixtures/fastapi_health_app
```

In the browser, use the Project form in the left sidebar.

Name:

```text
fastapi-health
```

Description:

```text
Fixture FastAPI project
```

Repository Path:

```text
/absolute/path/to/the/temp/fixture/repository
```

Default Branch:

```text
main
```

Click `Create Project`. The API observes the local Git repository and reconciles
the Project before returning it to the browser.

Browser checkpoint:

- The left sidebar shows the Project.
- The Project phase badge is `Ready`.
- The Project metadata shows one repository.

### 4. Create the Execution in the Browser

In the UI, select the Project and fill the New Execution form.

Name:

```text
add-health-endpoint
```

Goal:

```text
Create a minimal FastAPI application.
```

Description:

```text
GET /health returns {"status":"ok"}, add one automated test, add README
instructions, and do not add a database or authentication.
```

Acceptance Criteria:

```text
GET /health returns {"status":"ok"}.
One automated test is added.
README contains run instructions.
No database or authentication is added.
```

Submit the form, select the new draft Execution, and click `Run`.

Browser checkpoint:

- The Execution appears in the Execution list.
- The Execution phase moves from `Draft` to `Planning`.
- The Goal fields appear in the Execution overview.
- The Event timeline includes `GoalCreated`, `ExecutionPhaseChanged`, and
  `ExecutionRunStarted`.

### 5. Observe Planning in the Browser

The backend runner invokes the Planner runtime with the local Ollama Provider.
The Planner should produce a Plan with one Coding WorkItem:

```text
add-health
```

Browser checkpoint:

- The Execution moves to `WaitingForPlanApproval`.
- The Plan panel shows the proposed WorkItem.
- Artifacts include Planner prompt, model response, and Plan content.
- The Event timeline includes `PlannerRunStarted`, `PlanProduced`,
  `PlannerRunCompleted`, and `ApprovalRequested`.

### 6. Approve the Plan in the Browser

When the Plan approval appears in the Approvals panel, click `Approve`.

Browser checkpoint:

- The approval phase becomes `Approved`.
- The Event timeline records the human approval decision.

### 7. Materialize Work and Prepare the Workspace

After Plan approval, the backend runner reconciles the Plan and Execution,
creates the WorkItem, prepares a local Git worktree Workspace from the fixture
repository, attaches the Workspace to the WorkItem, and schedules the WorkItem.

Browser checkpoint:

- The Execution moves through `PreparingWorkspace` to `Executing`.
- The WorkItem becomes `Ready`, then `Scheduled`.
- The Workspace path is under `./data/mvp-demo-workspaces`, not inside the source
  checkout.

### 8. Observe the Coding Role

The backend runner invokes the Coding runtime against the prepared Workspace. For
the MVP scenario, the Coding Role should change only the Workspace and produce:

```text
app.py
tests/test_health.py
README.md
```

Browser checkpoint:

- Role Invocations show a succeeded Coding invocation.
- Artifacts include tool logs, a summary, and a Git diff.
- The source checkout remains clean when checked with `git status --short`.

### 9. Observe Independent Verification

The backend runner runs the verification controller for the WorkItem. The
verification command is:

```bash
python -m pytest -q
```

Browser checkpoint:

- The WorkItem moves to `Succeeded`.
- Artifacts include command output and a verification report.
- The verification report says all commands passed.

### 10. Observe Review and Repair

The backend runner reconciles the Execution into `Reviewing`, then invokes the
Reviewer runtime against the immutable diff, summary, and verification report
Artifacts.

If the Review returns `RequestChanges`, Maestro creates a repair WorkItem and
the runner continues through Coding, verification, and Review again.

Browser checkpoint:

- The first Review shows blocking findings.
- A repair WorkItem appears and references the Review.
- The final Review verdict is `Approve`.
- Review subjects point at exact Artifact resource versions.

### 11. Approve the Final Result in the Browser

When the final approval appears, click `Approve`.

Browser checkpoint:

- The Execution phase becomes `Completed`.
- The Event timeline shows every major transition.
- Artifacts contain prompts, responses, tool logs, diffs, verification reports,
  summaries, reviews, and the Plan.

### 12. Restart Recovery Check

Stop Maestro with `Ctrl-C`, then start it again with the same environment
variables from step 2. Reopen:

```text
http://127.0.0.1:7860/ui
```

Browser checkpoint:

- The Project, Execution, Events, Artifacts, Reviews, and Approvals are still
  present.
- A completed Execution remains `Completed`.
- If restarted before completion, the latest persisted phase and evidence are
  retained.

### Manual Step Map

| Step | Browser | Terminal or driver |
|---|---|---|
| Create Project | yes | initialize fixture Git repo |
| Create Execution | yes | optional API alternative |
| Plan generation | run/inspect | backend runner invokes PlannerRuntime |
| Plan approval | yes | optional API alternative |
| Workspace preparation | inspect | backend runner uses WorkspaceLifecycleService |
| Coding | inspect | backend runner invokes CodingRuntime |
| Verification | inspect | backend runner invokes VerificationController |
| Review | inspect | backend runner invokes ReviewerRuntime |
| Repair loop | approve/inspect | backend runner continues after review |
| Final approval | yes | optional API alternative |
| Restart recovery | inspect | restart server with same data paths |

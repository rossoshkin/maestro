# Maestro

**The Operating System for AI Teams**

Maestro is a local-first execution orchestration platform for coordinating specialized AI Roles across planning, implementation, review, and human approval.

## Run Maestro Locally

Maestro now includes the local MVP control plane: SQLite persistence, immutable
Events and Artifacts, resource controllers, Planner/Coding/Reviewer runtimes,
REST API, and the browser UI.

### 1. Install `uv`

Maestro uses `uv` for Python project management. On macOS with Homebrew:

```bash
brew install uv
```

Confirm it is available:

```bash
uv --version
```

### 2. Prepare Local Providers

For the MVP local workflow, run Ollama on localhost and make sure the Codex CLI
is configured:

```bash
export OLLAMA_HOST=http://127.0.0.1:11434
ollama list
codex --version
```

The deterministic MVP test harness does not call external models, but the real
local runtime expects Ollama and Codex provider configuration.

### 3. Install Project Dependencies

From the repository root:

```bash
uv sync
```

This creates a local `.venv/` and installs Maestro with its runtime and
development dependencies.

### 4. Check the CLI

```bash
uv run maestro --help
```

You should see the `serve` command listed.

### 5. Start Maestro

Use the CLI:

```bash
OLLAMA_HOST=http://127.0.0.1:11434 uv run maestro serve
```

By default Maestro binds to `127.0.0.1:7860`.

You can override the bind address or port:

```bash
uv run maestro serve --host 127.0.0.1 --port 8765
```

The API can also be started directly with Uvicorn:

```bash
uv run uvicorn maestro.presentation.api:app --host 127.0.0.1 --port 7860
```

### 6. Verify Health

In another terminal:

```bash
curl http://127.0.0.1:7860/health/live
curl http://127.0.0.1:7860/health/ready
```

Both endpoints should return:

```json
{"status":"ok"}
```

### 7. Open the UI

Open:

```text
http://127.0.0.1:7860/ui
```

Use the Project form in the left sidebar to create a Project, then use the New
Execution form in the main panel to create an Execution. Select the Execution
and click `Run`; a draft Execution starts planning, and an already-started
Execution resumes the local backend runner. The runner invokes Ollama/Codex,
prepares Workspaces, runs Coding, verifies changes, and stops only when human
approval is required or the Execution reaches a terminal phase. The UI shows
Plans, Work Items, Role Invocations, Artifacts, Reviews, Approvals, and the Event
timeline as the backend works.

### 8. Run the MVP Demo Harness

The end-to-end MVP harness creates a fixture Git repository, persists an
Execution, generates and approves a Plan, prepares an isolated Workspace, runs
Coding tools, verifies with pytest, performs a Reviewer-requested repair,
survives an application restart, and reaches Completed:

```bash
uv run pytest tests/e2e/test_mvp_vertical_slice.py
```

More demo notes are in
[MVP_DEMO.md](docs/development/MVP_DEMO.md).

### 9. Stop Maestro

Press `Ctrl-C` in the terminal running the server.

### Configuration

Maestro reads configuration from environment variables prefixed with
`MAESTRO_`.

| Variable | Default |
|---|---|
| `MAESTRO_DATABASE_URL` | `sqlite:///./data/maestro.db` |
| `MAESTRO_ARTIFACT_ROOT` | `./data/artifacts` |
| `MAESTRO_WORKSPACE_ROOT` | `./data/workspaces` |
| `MAESTRO_LOG_LEVEL` | `INFO` |
| `MAESTRO_BIND_ADDRESS` | `127.0.0.1` |
| `MAESTRO_PORT` | `7860` |

Example:

```bash
MAESTRO_LOG_LEVEL=DEBUG MAESTRO_PORT=8765 uv run maestro serve
```

## Development Checks

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pre-commit run --all-files
```

## Glossary

| Concept | Meaning |
|---|---|
| **Goal** | What the human wants to achieve. |
| **Execution** | One complete orchestration run. |
| **Workflow** | The state machine governing an Execution. |
| **Plan** | The strategy produced by the Planner Role. |
| **Work Item** | An individual unit of work derived from a Plan. |
| **Role** | A specialization such as Planner, Coding, Reviewer, or Researcher. |
| **Agent** | A runtime instance fulfilling a Role. |
| **Provider** | A bridge to a model or external service. |
| **Model** | The underlying language model. |
| **Artifact** | Any output produced during an Execution, such as diffs, logs, reviews, or reports. |
| **Knowledge Source** | A source of contextual information, such as Markdown, NAS, Git, Odysseus Documents, or Confluence. |
| **Workspace** | An isolated execution environment, typically backed by a Git worktree. |
| **Capability** | A permission or operation an Agent may use, such as `read_file` or `run_command`. |

## Architecture Documentation

- [01 — Vision](docs/architecture/01_Vision.md)
- [02 — Principles](docs/architecture/02_Principles.md)
- [03 — System Architecture](docs/architecture/03_System_Architecture.md)
- [04 — Domain Model](docs/architecture/04_Domain_Model.md)
- [05 — Workflows](docs/architecture/05_Workflows.md)
- [06 — Roles](docs/architecture/06_Roles.md)
- [07 — Execution](docs/architecture/07_Execution.md)
- [08 — Capabilities](docs/architecture/08_Capabilities.md)
- [09 — Workspace](docs/architecture/09_Workspace.md)
- [10 — Knowledge](docs/architecture/10_Knowledge.md)
- [11 — Providers](docs/architecture/11_Providers.md)


- [12 — Event System](docs/architecture/12_Event_System.md)
- [13 — State Machine](docs/architecture/13_State_Machine.md)
- [14 — Persistence](docs/architecture/14_Persistence.md)


- [15 — Web API](docs/architecture/15_Web_API.md)
- [16 — Web UI](docs/architecture/16_Web_UI.md)

- [17 — Security](docs/architecture/17_Security.md)
- [18 — Extensibility](docs/architecture/18_Extensibility.md)
- [19 — Deployment](docs/architecture/19_Deployment.md)
- [20 — Roadmap](docs/architecture/20_Roadmap.md)

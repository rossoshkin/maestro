# Maestro

**The Operating System for AI Teams**

Maestro is a local-first execution orchestration platform for coordinating specialized AI Roles across planning, implementation, review, and human approval.

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

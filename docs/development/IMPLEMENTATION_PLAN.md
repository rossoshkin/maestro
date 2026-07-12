# Maestro MVP Implementation Plan

Status: Living document

Current milestone: Milestone 18 — Planner Role Runtime

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

# Milestone 2 — Project Resource

Status: Complete on 2026-07-11.

## Goal

Implement the `Project` resource as the root configuration object for a Maestro project.

A Project defines:

- repositories;
- Workflow binding;
- Role bindings;
- Knowledge Source bindings;
- Project policies;
- default configuration.

A Project does not own runtime Execution state.

## Architecture References

- `docs/architecture/04_Domain_Model.md`
- `docs/architecture/10_Knowledge.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 1 — Common Resource Infrastructure

## Deliverables

- `Project`
- `ProjectSpec`
- `ProjectStatus`
- repository binding models
- Role binding models
- Knowledge Source binding models
- Project policy models
- Project validation
- Project repository interface
- Project persistence implementation
- Project service
- serialization support
- optimistic concurrency support

## Acceptance Criteria

- valid Projects serialize and deserialize correctly;
- invalid repository bindings are rejected;
- duplicate repository IDs are rejected;
- missing Workflow bindings are rejected when required;
- `generation` increments only when `spec` changes;
- `resourceVersion` increments on every mutation;
- stale updates return conflict errors;
- deleting or archiving a Project never deletes source repositories.

## Tests

- model validation tests;
- repository binding tests;
- persistence round-trip tests;
- optimistic concurrency tests;
- generation tests;
- resource version tests;
- archive behavior tests.

## Out of Scope

- Execution;
- Workflow implementation;
- controllers;
- REST API;
- UI;
- actual repository cloning.

## Exit Criteria

The Project resource is fully compliant with `RESOURCE_SPECIFICATION.md` and can be persisted reliably.

## Completion Notes

- Added `Project`, `ProjectSpec`, `ProjectStatus`, repository bindings, Role bindings, Knowledge Source bindings and Project policy models.
- Added structural validation for repository bindings, Workflow references, duplicate repository IDs, duplicate Knowledge Source bindings and Project ownership.
- Added Project repository protocol and SQLite persistence implementation.
- Added Project service operations for create, spec update, archival and deletion requests.
- Preserved repository safety: archive and deletion requests do not modify source repository paths.
- Implemented optimistic concurrency through `resourceVersion`.
- Preserved generation semantics: Project spec changes increment `generation`; status and deletion timestamp changes do not.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`
  - `uv run pre-commit run --all-files`

---

# Milestone 3 — Execution Resource

Status: Complete on 2026-07-12.

## Goal

Implement `Execution` as Maestro's primary aggregate root.

An Execution represents one complete attempt to satisfy one Goal.

## Architecture References

- `docs/architecture/04_Domain_Model.md`
- `docs/architecture/07_Execution.md`
- `docs/architecture/13_State_Machine.md`
- `docs/adr/0002-execution-aggregate.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 1
- Milestone 2

## Deliverables

- `Goal`
- `Execution`
- `ExecutionSpec`
- `ExecutionStatus`
- Execution phase enum
- transition validation
- Execution repository interface
- persistence implementation
- Execution service
- owner-reference validation
- limit configuration
- suspension and cancellation fields

## Acceptance Criteria

- every Execution has exactly one Goal;
- Execution references exactly one Project;
- Execution references exactly one Workflow version;
- illegal phase transitions are rejected;
- terminal Executions cannot resume implicitly;
- Goal mutation after Planning is rejected;
- status remains controller-owned;
- persistence survives restart;
- owner references validate correctly.

## Tests

- phase transition tests;
- terminal state tests;
- Goal immutability tests;
- owner reference tests;
- persistence tests;
- invalid Workflow reference tests;
- cancellation request validation.

## Out of Scope

- controller reconciliation;
- Workflow execution;
- Planner;
- Work Items;
- API;
- UI.

## Exit Criteria

Execution lifecycle and persistence are complete and fully validated.

## Completion Notes

- Added `Goal`, `Execution`, `ExecutionSpec`, `ExecutionStatus`, limit configuration, suspension and cancellation desired-state fields.
- Added the Execution phase enum and the valid transition matrix from `RESOURCE_SPECIFICATION.md`.
- Added owner-reference validation requiring exactly one matching Project controller owner.
- Added Execution repository protocol and SQLite persistence implementation.
- Added Execution service operations for creation, spec updates, cancellation requests and suspension changes.
- Enforced Project readiness on Execution creation through the Project repository.
- Enforced Goal immutability after the Execution leaves `Draft`.
- Enforced terminal Execution behavior: terminal phases do not transition except allowed archival paths.
- Preserved generation semantics: Execution spec changes increment `generation`; status changes do not.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`
  - `uv run pre-commit run --all-files`

---

# Milestone 4 — Workflow Resource

Status: Complete on 2026-07-12.

## Goal

Implement immutable, versioned Workflow definitions.

A Workflow describes desired orchestration state. It does not execute code.

## Architecture References

- `docs/architecture/05_Workflows.md`
- `docs/architecture/13_State_Machine.md`
- `docs/adr/0001-control-plane.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 1
- Milestone 3

## Deliverables

- `Workflow`
- `WorkflowSpec`
- `WorkflowStatus`
- Workflow step models
- step-type enum
- transition graph
- graph validator
- terminal-step validation
- retry policy model
- approval-step model
- Role-step model
- system-step model
- fan-out model
- decision-step model
- immutable versioning support
- Workflow repository

## Acceptance Criteria

- duplicate step IDs are rejected;
- invalid entrypoints are rejected;
- missing transition targets are rejected;
- unreachable terminal states are rejected;
- unbounded cycles are rejected;
- retry counts must be finite;
- Workflow versions are immutable;
- Executions can pin exact Workflow versions;
- Provider-specific details are prohibited.

## Tests

- graph validation tests;
- cycle detection tests;
- terminal reachability tests;
- version immutability tests;
- serialization tests;
- invalid Role reference tests.

## Out of Scope

- Workflow controllers;
- scheduler;
- model invocation;
- UI Workflow editor.

## Exit Criteria

Workflow definitions can be registered, validated, versioned, and referenced by Executions.

## Completion Notes

- Added `Workflow`, `WorkflowSpec`, `WorkflowStatus`, step models and supported step-type enums.
- Added graph validation for duplicate step IDs, entrypoints, transition targets, terminal reachability, unreachable steps and unbounded cycles.
- Added finite retry policy modeling and bounded-cycle validation for repair loops.
- Added structural Role references that require Role name and version while prohibiting Agent, Provider or tool-specific fields.
- Added immutable Workflow version behavior: existing Workflow specs cannot be changed; new versions are registered as distinct resources.
- Added Workflow repository protocol and SQLite persistence implementation with exact `namespace/name/version` lookup.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`
  - `uv run pre-commit run --all-files`

---

# Milestone 5 — Plan Resource

Status: Complete on 2026-07-12.

## Goal

Implement immutable, versioned Plans generated for an Execution.

A Plan decomposes a Goal into Work Items.

## Architecture References

- `docs/architecture/04_Domain_Model.md`
- `docs/architecture/05_Workflows.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 3
- Milestone 4

## Deliverables

- `Plan`
- `PlanSpec`
- `PlanStatus`
- assumptions
- questions
- risks
- Plan versioning
- Plan validation
- Work Item proposal models
- Plan repository
- approval-ready state
- Plan supersession support

## Acceptance Criteria

- Plans belong to exactly one Execution;
- Work Item IDs are unique within a Plan;
- dependency graphs are acyclic;
- every proposed Work Item has a Role;
- every proposed Work Item has acceptance criteria;
- approved Plans are immutable;
- rejected Plans are superseded by new versions;
- only one approved Plan exists per Execution.

## Tests

- Plan validation;
- dependency-cycle tests;
- immutable approved Plan tests;
- supersession tests;
- duplicate ID tests;
- missing acceptance-criteria tests.

## Out of Scope

- Planner model integration;
- human approval UI;
- Work Item execution.

## Exit Criteria

Plans can be created, validated, versioned, approved, rejected, and superseded.

## Completion Notes

- Added `Plan`, `PlanSpec`, `PlanStatus`, Plan phases, validation results, Role references, risks and Work Item proposal models.
- Added Plan validation for Execution ownership, positive versions, unique Work Item IDs, required Role references, required acceptance criteria, dependency target existence and acyclic dependency graphs.
- Added immutable Plan version behavior: existing Plan specs cannot be changed; new versions are registered as distinct resources and can reference the revision they supersede.
- Added Plan lifecycle transition validation for approval, rejection, invalidation and supersession states.
- Added approval-ready status support and human audit metadata requirements for approved and rejected Plans.
- Added Plan repository protocol and SQLite persistence implementation with exact Execution/version lookup.
- Enforced one approved Plan per Execution at persistence level.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`

---

# Milestone 6 — WorkItem Resource

Status: Complete on 2026-07-12.

## Goal

Implement `WorkItem` as the smallest schedulable unit of work.

## Architecture References

- `docs/architecture/04_Domain_Model.md`
- `docs/architecture/05_Workflows.md`
- `docs/architecture/06_Roles.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 3
- Milestone 5

## Deliverables

- `WorkItem`
- `WorkItemSpec`
- `WorkItemStatus`
- dependency references
- requested Capabilities
- retry policy
- acceptance criteria
- verification commands
- Work Item repository
- readiness evaluation
- transition validation

## Acceptance Criteria

- every Work Item belongs to one Execution;
- every Work Item belongs to one Plan version;
- every Work Item references exactly one Role;
- Work Items become Ready only after dependencies succeed;
- failed dependencies block dependent Work Items;
- retries are finite;
- Agent output cannot directly mark Work Item success;
- verification evidence is required for success where configured.

## Tests

- dependency readiness tests;
- blocked-state tests;
- retry-limit tests;
- transition tests;
- repository tests;
- invalid Role reference tests.

## Out of Scope

- scheduler;
- actual execution;
- Agent assignment;
- model calls.

## Exit Criteria

Work Items are fully modeled, persisted, and readiness can be determined deterministically.

## Completion Notes

- Added `WorkItem`, `WorkItemSpec`, `WorkItemStatus`, WorkItem phases, retry policy, Role references, Plan revision references, dependency references, requested Capabilities and verification evidence models.
- Added WorkItem validation for Execution ownership, exact Plan revision binding, required Role references, required acceptance criteria, duplicate dependency references, duplicate requested Capabilities, self-dependencies and finite retry limits.
- Added deterministic readiness evaluation for Pending WorkItems using persisted dependency snapshots.
- Added dependency readiness behavior for waiting dependencies, missing dependencies, failed dependencies, blocked dependencies and dependency scope mismatches.
- Added WorkItem transition validation, including bounded retry behavior after failure and terminal-state protection.
- Added success evidence validation so Agent result artifacts alone cannot mark a WorkItem succeeded when verification commands are configured.
- Added limited spec update behavior: execution, plan, planner Work Item ID and Role bindings are immutable, and specs cannot change after execution starts.
- Added WorkItem repository protocol and SQLite persistence implementation with Execution, Plan and Plan Work Item ID lookup support.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`

---

# Milestone 7 — Role Resource

Status: Complete on 2026-07-12.

## Goal

Implement versioned, declarative Roles.

A Role defines responsibility, schemas, Capabilities, and policies.

## Architecture References

- `docs/architecture/06_Roles.md`
- `docs/adr/0003-role-vs-agent.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 1
- Milestone 6

## Deliverables

- `Role`
- `RoleSpec`
- `RoleStatus`
- input schema reference
- output schema reference
- prompt reference
- required Capabilities
- optional Capabilities
- prohibited Capabilities
- execution policy
- Role validation
- Role repository
- immutable Role versioning

## Acceptance Criteria

- Roles do not reference models;
- Roles do not reference Providers;
- Roles cannot request Workflow transition permissions;
- input and output schema references are required;
- Role versions are immutable;
- prohibited Capabilities override optional and required Capabilities;
- invalid Capability combinations are rejected.

## Tests

- Role validation;
- immutable version tests;
- prohibited Capability tests;
- schema reference tests;
- model/provider independence tests.

## Out of Scope

- Agent runtime;
- prompt rendering;
- model invocation.

## Exit Criteria

Roles can be defined, validated, versioned, and referenced by Workflows and Work Items.

## Completion Notes

- Added shared `CapabilityName` validation for Role policies and WorkItem requested Capabilities.
- Added `Role`, `RoleSpec`, `RoleStatus`, Role phases, validation results, prompt references, schema references and execution policy models.
- Added Role validation for required input and output schema references, bounded execution policy, duplicate Capabilities and invalid required/optional/prohibited Capability combinations.
- Enforced provider and model independence through strict Role schema validation.
- Enforced that Roles cannot request Workflow transition Capabilities while allowing them to explicitly prohibit those Capabilities.
- Added effective Capability helpers so prohibited Capabilities deny optional grants.
- Added immutable Role version behavior: existing Role specs cannot be changed; new versions are registered as distinct resources.
- Added Role repository protocol and SQLite persistence implementation with exact `namespace/name/version` lookup.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`

---

# Milestone 8 — Agent Resource

Status: Complete on 2026-07-12.

## Goal

Implement Agents as operational runtime configurations that fulfill compatible Roles.

## Architecture References

- `docs/architecture/06_Roles.md`
- `docs/adr/0003-role-vs-agent.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 7
- Milestone 10

## Deliverables

- `Agent`
- `AgentSpec`
- `AgentStatus`
- supported Role versions
- provider reference
- model identifier
- capacity
- scheduling labels
- readiness state
- Agent repository
- Role compatibility validator

## Acceptance Criteria

- Agents declare supported Role versions;
- Agents reference Providers, not Provider implementations;
- Agent state is operational only;
- Agent does not contain project knowledge;
- incompatible Role assignment is rejected;
- unavailable Providers make Agents unavailable or degraded;
- capacity limits are represented.

## Tests

- Role compatibility tests;
- Provider readiness tests;
- capacity tests;
- Agent state tests;
- invalid model/provider binding tests.

## Out of Scope

- scheduler;
- actual invocation;
- dynamic capacity allocation.

## Exit Criteria

Agents can be registered, validated, and matched against Role requirements.

## Completion Notes

- Added `Agent`, `AgentSpec`, `AgentStatus`, Agent phases, Provider references, supported Role versions, CapabilityBinding references, capacity and scheduling configuration models.
- Added Agent validation for provider references, model identifiers, duplicate supported Roles, duplicate supported Role versions, duplicate CapabilityBinding references and capacity-sensitive status.
- Preserved the Role/Agent boundary: Agents reference Providers by resource name and do not embed Provider implementation details.
- Preserved operational-only Agent semantics: Agent specs reject project references, knowledge bindings and other project memory fields.
- Added Role compatibility evaluation for Role readiness, Role name support and exact Role version support.
- Added Provider readiness evaluation for provider mismatch, pending/degraded/unavailable Provider state, unavailable model state, disabled scheduling and busy capacity.
- Added Agent repository protocol and SQLite persistence implementation with Provider lookup and Role-version compatibility listing.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`

---

# Milestone 9 — Capability System

Status: Complete on 2026-07-12.

## Goal

Implement Maestro's capability-based authorization model.

Capabilities define permissions. Tools implement Capabilities.

## Architecture References

- `docs/architecture/08_Capabilities.md`
- `docs/architecture/17_Security.md`
- `docs/adr/0004-capabilities.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 7
- Milestone 8

## Deliverables

- `Capability`
- `CapabilityBinding`
- side-effect levels
- Capability scopes
- grant rules
- deny rules
- Capability resolver
- admission checks
- effective Capability calculation
- policy-denial errors
- Capability repository

## Acceptance Criteria

- deny-by-default behavior works;
- explicit deny overrides grant;
- Agents cannot self-grant Capabilities;
- required Capabilities must be available before scheduling;
- Planner cannot receive filesystem-write or shell-execute;
- Reviewer cannot receive filesystem-write;
- destructive Capabilities require explicit policy.

## Tests

- deny precedence tests;
- effective Capability resolution tests;
- Planner policy tests;
- Reviewer policy tests;
- missing required Capability tests;
- scope tests.

## Out of Scope

- tool implementation;
- shell execution;
- remote authorization.

## Exit Criteria

Capability admission is deterministic, tested, and usable by scheduler and runtime.

## Completion Notes

- Expanded shared Capability primitives into first-class `Capability` and `CapabilityBinding` resources.
- Added side-effect levels, approval policies, scopes, Capability phases and CapabilityBinding phases.
- Added validation for canonical Capability names, schema references, duplicate grants/denies, duplicate scopes and sensitive destructive/privileged Capability policy requirements.
- Added scoped CapabilityBinding matching with workspace label selectors.
- Added deterministic Capability resolution with deny-by-default semantics, explicit grant handling, explicit deny precedence, ready Capability catalog checks and Agent-supported Capability filtering.
- Added policy-denial modeling through structured violations and `CapabilityPolicyDeniedError`.
- Enforced scheduler-admission rules: required Capabilities must be granted and Ready, Agents cannot self-grant through Agent-owned bindings, Planner cannot receive filesystem-write or shell-execute Capabilities, and Reviewer cannot receive filesystem-write Capabilities.
- Added Capability and CapabilityBinding repository protocols plus SQLite persistence implementations with canonical-name lookup and Ready binding listing.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`

---

# Milestone 10 — Provider Abstraction

Status: Complete on 2026-07-12.

## Goal

Implement model Provider interfaces and Provider resources.

## Architecture References

- `docs/architecture/11_Providers.md`
- `docs/adr/0007-provider-interface.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 1
- Milestone 8

## Deliverables

- Provider resource models
- Provider interface
- health contract
- model discovery contract
- structured generation contract
- tool-loop contract
- timeout handling
- Provider errors
- mock Provider
- Provider repository
- Provider health service

## Acceptance Criteria

- domain code has no Provider-specific imports;
- unavailable Providers report structured health status;
- Provider capabilities are discoverable;
- structured output contract is model-agnostic;
- timeout and failure errors are normalized;
- mock Provider supports deterministic tests.

## Tests

- Provider contract tests;
- health tests;
- timeout tests;
- normalized-error tests;
- model discovery tests;
- mock Provider tests.

## Out of Scope

- Ollama implementation;
- Codex implementation;
- scheduler;
- actual Role execution.

## Exit Criteria

Maestro has a stable model-agnostic Provider contract.

## Completion Notes

- Added shared `ModelIdentifier` validation for Agent and Provider model references.
- Added `Provider`, `ProviderSpec`, `ProviderStatus`, Provider phases, data policy, feature discovery, auth references, normalized failure details and allowed-model filtering.
- Added model-agnostic Provider runtime contracts for health, model discovery, structured generation and tool-loop execution.
- Added normalized Provider operation errors and timeout normalization.
- Added `ProviderHealthService` to refresh Provider status from runtime health/model discovery without depending on a concrete Provider implementation.
- Added deterministic `MockProvider` for structured generation, model discovery, tool-loop, unavailable-model and timeout tests.
- Added Provider repository protocol and SQLite persistence implementation with namespace/name lookup.
- Preserved Provider abstraction boundaries: domain code models Provider type as data and does not import Ollama, Codex, OpenAI or other concrete providers.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`

---

# Milestone 11 — Workspace Abstraction

Status: Complete on 2026-07-12.

## Goal

Implement Workspace resource, provider interface, and lifecycle.

## Architecture References

- `docs/architecture/09_Workspace.md`
- `docs/architecture/17_Security.md`
- `docs/adr/0006-workspaces.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 2
- Milestone 3
- Milestone 9

## Deliverables

- Workspace resource
- Workspace provider interface
- Workspace lifecycle service
- local Git worktree provider
- branch-per-Execution behavior
- path containment
- symlink escape protection
- cleanup finalizer
- Workspace locking
- diff collection
- status collection
- local command execution interface

## Acceptance Criteria

- Workspace never modifies the source checkout directly;
- path traversal is rejected;
- symlink escape is rejected;
- cleanup never deletes the source repository;
- one Execution can create an isolated worktree;
- Workspace can collect Git status and diff;
- failed cleanup preserves diagnostic state;
- Workspace lifecycle survives restart.

## Tests

- worktree creation tests;
- path traversal tests;
- symlink escape tests;
- cleanup tests;
- source repository protection tests;
- locking tests;
- lifecycle tests.

## Out of Scope

- remote workers;
- containers;
- VMs;
- network policy enforcement beyond local MVP limits.

## Exit Criteria

A safe local Workspace can be created, used, inspected, and cleaned.

## Completion Notes

- Added `Workspace`, `WorkspaceSpec`, `WorkspaceStatus`, lifecycle phases, cleanup finalizer enforcement, Execution ownership validation and Workspace repository protocol.
- Added Workspace path safety helpers that reject path traversal and symlink escapes.
- Added Workspace locking helpers with optimistic concurrency.
- Added SQLite Workspace persistence with list-by-Execution support and restart-safe serialized snapshots.
- Added `WorkspaceLifecycleService` for prepare, state refresh, diff collection, command execution and cleanup flows.
- Added a local Git worktree provider that creates branch-per-Execution worktrees, preserves source checkouts, collects Git status/diff and refuses unsafe cleanup targets.
- Added domain, persistence, lifecycle and real Git provider tests covering traversal, symlink escape, source protection, cleanup diagnostics, locking and restart behavior.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`

---

# Milestone 12 — Artifact and Event Resources

Status: Complete on 2026-07-12.

## Goal

Implement immutable Artifacts and Events.

## Architecture References

- `docs/architecture/12_Event_System.md`
- `docs/architecture/14_Persistence.md`
- `docs/adr/0005-event-model.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 1
- Milestone 3
- Milestone 6

## Deliverables

### Artifact

- Artifact model
- immutable storage metadata
- checksum validation
- provenance
- Artifact repository
- local Artifact storage

### Event

- Event model
- sequence numbering per Execution
- append-only Event store
- correlation IDs
- Event publisher interface
- Event querying

## Acceptance Criteria

- Artifacts are immutable;
- Artifact checksum mismatches are detected;
- Events are append-only;
- Events are ordered within an Execution;
- duplicate Event delivery does not corrupt state;
- resource state remains authoritative;
- provenance is always recorded.

## Tests

- Artifact integrity tests;
- immutable Artifact tests;
- Event sequence tests;
- duplicate Event tests;
- Event persistence tests;
- provenance tests.

## Out of Scope

- distributed Event bus;
- Kafka;
- object storage;
- webhook delivery.

## Exit Criteria

Maestro can persist evidence and audit history reliably.

## Completion Notes

- Added immutable `Artifact` resources with Execution ownership, required provenance, storage metadata, SHA-256 validation, source references and integrity phases.
- Added Artifact integrity status helpers that mark content as `Available`, `Corrupt` or `Missing` from storage evidence.
- Added SQLite Artifact persistence with immutable spec updates, status updates, list-by-Execution and list-by-WorkItem support.
- Added local filesystem Artifact storage with path containment, checksum calculation, readback, missing-content detection and overwrite protection.
- Added `ArtifactService` to write bytes, persist immutable metadata and refresh integrity status.
- Added immutable `Event` resources, `EventDraft`, query filters, publisher and append-only Event store contracts.
- Added SQLite Event store with per-Execution sequence numbering, correlation queries and idempotent duplicate delivery handling.
- Added domain, persistence, storage and application tests for Artifact integrity, immutability, Event ordering, duplicate delivery, provenance and restart behavior.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`

---

# Milestone 13 — Approval and Review Resources

Status: Complete on 2026-07-12.

## Goal

Implement first-class Approval and Review resources.

## Architecture References

- `docs/architecture/05_Workflows.md`
- `docs/architecture/06_Roles.md`
- `docs/architecture/17_Security.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 5
- Milestone 12

## Deliverables

### Approval

- Approval model
- immutable decisions
- approval subject versioning
- invalidation rules
- approval repository

### Review

- Review model
- verdicts
- blocking findings
- non-blocking findings
- missing evidence
- Review repository

## Acceptance Criteria

- approvals reference exact resource versions;
- changed subjects invalidate approvals;
- decisions are attributable;
- Reviewer cannot mutate Workspace;
- Review subjects are immutable Artifacts;
- verdicts validate;
- blocking and non-blocking findings remain distinct.

## Tests

- approval invalidation tests;
- exact-subject-version tests;
- immutable decision tests;
- Review verdict tests;
- missing evidence tests;
- Reviewer read-only policy tests.

## Out of Scope

- Codex review invocation;
- UI approval buttons;
- automated approval policies.

## Exit Criteria

Approvals and Reviews are fully modeled and persisted.

## Completion Notes

- Added `Approval` resources with exact subject `resourceVersion` references, Execution ownership, approval phases, attributable decisions and immutable decision-history validation.
- Added Approval invalidation rules for changed, deleted or mismatched subjects.
- Added `ApprovalService` for persisting decisions and subject invalidation checks.
- Added SQLite Approval persistence with list-by-Execution, list-by-subject, immutable spec updates and optimistic concurrency.
- Added `Review` resources with exact Artifact subject references, read-only reviewer policy, verdict semantics, structured findings, missing evidence and completion metadata.
- Added validation that Reviewer policy cannot allow Workspace mutation and that blocking/non-blocking findings remain distinct.
- Added SQLite Review persistence with list-by-Execution, list-by-WorkItem, immutable spec updates and optimistic concurrency.
- Added domain, persistence and application tests for approval invalidation, exact subject versions, immutable decisions, review verdicts, missing evidence and reviewer read-only policy.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`

---

# Milestone 14 — Controller Framework

Status: Complete on 2026-07-12.

## Goal

Implement the generic reconciliation framework used by Maestro controllers.

## Architecture References

- `docs/architecture/03_System_Architecture.md`
- `docs/architecture/05_Workflows.md`
- `docs/architecture/12_Event_System.md`
- `docs/architecture/13_State_Machine.md`

## Dependencies

- Milestones 2–13

## Deliverables

- base controller protocol
- reconciliation context
- controller registry
- reconcile queue
- retry policy
- optimistic concurrency handling
- status writer
- Condition helper
- Event publisher integration
- controller lifecycle
- restart recovery

## Acceptance Criteria

- reconciliation is idempotent;
- duplicate reconciliation is safe;
- stale resource updates retry correctly;
- controller failures preserve evidence;
- status updates increment resourceVersion but not generation;
- meaningful transitions emit Events;
- restart resumes unfinished resources.

## Tests

- idempotency tests;
- duplicate reconciliation tests;
- stale-version tests;
- restart recovery tests;
- status ownership tests;
- Event emission tests.

## Out of Scope

- model reasoning;
- scheduler assignment logic;
- API;
- UI.

## Exit Criteria

Maestro can run deterministic, restart-safe controllers.

## Completion Notes

- Added a generic `Controller` protocol, `ReconciliationContext`, `ReconcileResult` and `ReconcileRun` outcome model.
- Added `ControllerRegistry` with deterministic listing and duplicate-kind protection.
- Added a deduplicating FIFO `ReconcileQueue` and `ControllerRuntime` with start/stop lifecycle, retry handling and restart recovery for unfinished resources.
- Added `RetryPolicy` for finite controller retries and optimistic-concurrency retry bounds.
- Added `StatusWriter` that reloads resources on stale `resourceVersion`, writes status through repositories and preserves generation semantics.
- Added Condition helpers for `observedGeneration`, single-condition replacement and stable `lastTransitionTime` when condition status does not change.
- Added deterministic phase-transition Event emission through the Event publisher boundary.
- Added framework tests for idempotency, duplicate reconciliation, stale-version retry, restart recovery, status ownership, Event emission, runtime failure evidence and lifecycle behavior.
- Verification completed:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src`

---

# Milestone 15 — Resource Controllers

Status: Complete on 2026-07-12.

## Goal

Implement the MVP resource-specific controllers.

## Architecture References

- `docs/architecture/05_Workflows.md`
- `docs/architecture/07_Execution.md`
- `docs/architecture/13_State_Machine.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestone 14

## Deliverables

- ProjectController
- ExecutionController
- WorkflowController
- PlanController
- WorkItemController
- WorkspaceController
- ApprovalController
- ReviewController
- ArtifactController
- ProviderController
- AgentController

## Acceptance Criteria

- each controller owns only its resource status;
- subordinate resources are created idempotently;
- duplicate resources are not produced;
- Conditions are updated consistently;
- Execution advances only when evidence exists;
- cancellation is reconciled safely;
- terminal phases remain terminal.

## Tests

- controller-specific reconciliation tests;
- duplicate subordinate-resource tests;
- cancellation tests;
- terminal state tests;
- Condition update tests;
- restart tests.

## Out of Scope

- Planner implementation;
- coding execution;
- reviewer implementation;
- web UI.

## Exit Criteria

The resource graph can reconcile without model integrations.

## Completion Notes

- Added MVP resource controllers in `maestro.application.resource_controllers`.
- Implemented status-only reconciliation for Project, Workflow, Workspace, Approval, Review, Artifact, Provider and Agent resources.
- Implemented Plan approval reconciliation with exact resource-version matching.
- Implemented idempotent WorkItem materialization from approved Plans, including dependency reference reconciliation without duplicate WorkItems.
- Implemented WorkItem readiness reconciliation from dependency evidence and retry exhaustion handling.
- Implemented Execution phase reconciliation from persisted Plan, Workspace, WorkItem, Review and Approval evidence.
- Preserved terminal Execution phases and safe cancellation reconciliation.
- Added controller-specific tests covering idempotent subordinate creation, Conditions, evidence-driven Execution advancement, cancellation and terminal no-op behavior.
- Verification completed:
  - `env UV_CACHE_DIR=.uv-cache uv run pytest`
  - `env UV_CACHE_DIR=.uv-cache uv run ruff check .`
  - `env UV_CACHE_DIR=.uv-cache uv run mypy src`
  - `env UV_CACHE_DIR=.uv-cache PRE_COMMIT_HOME=.pre-commit-cache uv run pre-commit run --all-files`

---

# Milestone 16 — Scheduler

Status: Complete on 2026-07-12.

## Goal

Implement Agent selection and Work Item assignment.

## Architecture References

- `docs/architecture/03_System_Architecture.md`
- `docs/architecture/06_Roles.md`
- `docs/architecture/08_Capabilities.md`

## Dependencies

- Milestone 8
- Milestone 9
- Milestone 10
- Milestone 14

## Deliverables

- scheduler service
- Agent eligibility evaluation
- Role compatibility checks
- Capability admission
- Provider health checks
- capacity checks
- Agent assignment
- scheduling failure reasons
- deterministic selection policy

## Acceptance Criteria

- incompatible Agents are rejected;
- unhealthy Providers are avoided;
- required Capabilities must resolve;
- capacity limits are respected;
- scheduler never selects by hard-coded model name;
- scheduling decisions are logged and auditable;
- no eligible Agent results in structured blocked state.

## Tests

- Agent compatibility tests;
- capacity tests;
- unhealthy Provider tests;
- Capability admission tests;
- deterministic-selection tests;
- no-eligible-Agent tests.

## Out of Scope

- distributed queues;
- GPU-aware scheduling;
- remote workers;
- parallel execution.

## Exit Criteria

Ready Work Items can be assigned to eligible local Agents deterministically.

## Completion Notes

- Added deterministic WorkItem scheduling in `maestro.application.scheduler`.
- Implemented Agent eligibility evaluation across Role compatibility, Provider readiness, model availability, capacity and Capability admission.
- Enforced deny-by-default Capability resolution using Ready CapabilityBindings referenced by Agents.
- Implemented deterministic Agent selection by eligibility, priority, current assignment count and name.
- Implemented Ready WorkItem assignment through `assignedAgentRef` and the Ready to Scheduled transition.
- Implemented structured blocked Scheduling conditions when no eligible Agent can accept a WorkItem.
- Added auditable scheduler decision events for scheduled and blocked outcomes.
- Added scheduler tests covering compatible assignment, capacity, unhealthy Providers, Capability denial, deterministic selection and no-eligible-Agent blocking.
- Verification completed:
  - `env UV_CACHE_DIR=.uv-cache uv run pytest`
  - `env UV_CACHE_DIR=.uv-cache uv run ruff check .`
  - `env UV_CACHE_DIR=.uv-cache uv run ruff format --check .`
  - `env UV_CACHE_DIR=.uv-cache uv run mypy src`
  - `env UV_CACHE_DIR=.uv-cache PRE_COMMIT_HOME=.pre-commit-cache uv run pre-commit run --all-files`

---

# Milestone 17 — Ollama Provider

Status: Complete on 2026-07-12.

## Goal

Implement the initial local model Provider for Planner and Coding Roles.

## Architecture References

- `docs/architecture/11_Providers.md`
- `docs/architecture/17_Security.md`
- `docs/adr/0007-provider-interface.md`

## Dependencies

- Milestone 10

## Deliverables

- Ollama Provider adapter
- health check
- model listing
- structured output
- tool-calling loop support
- request timeout
- normalized Provider errors
- retry-safe behavior
- configuration validation

## Acceptance Criteria

- Provider health reflects actual endpoint state;
- models can be listed;
- structured Planner output can be parsed;
- tool calls can be exchanged;
- endpoint failures return normalized errors;
- Provider-specific code remains in infrastructure;
- tests use fake Ollama responses.

## Tests

- health tests;
- model listing tests;
- structured output tests;
- tool-call tests;
- timeout tests;
- malformed-response tests.

## Out of Scope

- Codex;
- remote worker execution;
- model fallback.

## Exit Criteria

Local Ollama models can be invoked through the generic Provider interface.

## Completion Notes

- Added `OllamaProvider` in `maestro.infrastructure.providers.ollama`.
- Kept Ollama-specific HTTP payloads and endpoint handling behind the generic `ModelProvider` interface.
- Implemented `/api/tags` health/model discovery with Ready, Degraded and Unavailable failure mapping.
- Implemented structured JSON generation through `/api/chat` with schema-aware `format` payloads.
- Implemented tool-call exchange through `/api/chat` with tool definition translation and tool-call validation.
- Added request timeout capping and normalized Provider errors for timeout, unavailable endpoint, invalid request, missing model, malformed structured output and invalid tool calls.
- Added infrastructure tests using a fake Ollama transport; no live Ollama daemon is required.
- Verification completed:
  - `env UV_CACHE_DIR=.uv-cache uv run pytest`
  - `env UV_CACHE_DIR=.uv-cache uv run ruff check .`
  - `env UV_CACHE_DIR=.uv-cache uv run mypy src`

---

# Milestone 18 — Planner Role Runtime

## Goal

Implement Planner Role invocation through Ollama.

## Architecture References

- `docs/architecture/05_Workflows.md`
- `docs/architecture/06_Roles.md`
- `docs/architecture/10_Knowledge.md`

## Dependencies

- Milestone 5
- Milestone 7
- Milestone 12
- Milestone 15
- Milestone 17

## Deliverables

- Planner prompt template
- Planner input builder
- Planner output schema
- Plan parsing
- one bounded repair attempt for invalid output
- Plan Artifact creation
- question handling
- Plan resource creation
- planning RoleInvocation records

## Acceptance Criteria

- Planner receives no write or shell Capabilities;
- Planner output validates against schema;
- invalid output gets one bounded repair attempt;
- valid output creates immutable Plan and Artifacts;
- blocking questions move Execution to WaitingForUserInput;
- Planner never writes status directly;
- all prompts and outputs are persisted.

## Tests

- valid Plan tests;
- invalid JSON tests;
- repair-attempt tests;
- question-routing tests;
- Capability denial tests;
- Artifact creation tests.

## Out of Scope

- Knowledge retrieval beyond explicit supplied context;
- coding execution;
- UI.

## Exit Criteria

A Goal can produce a valid Plan and approval request.

---

# Milestone 19 — Coding Tool Runtime

## Goal

Implement safe tool execution for the Coding Role.

## Architecture References

- `docs/architecture/08_Capabilities.md`
- `docs/architecture/09_Workspace.md`
- `docs/architecture/17_Security.md`

## Dependencies

- Milestone 9
- Milestone 11
- Milestone 12
- Milestone 17

## Deliverables

- list-files tool
- read-file tool
- write-file tool
- edit-file tool
- run-command tool
- Git status tool
- Git diff tool
- tool schema registry
- Capability enforcement
- timeout handling
- output truncation
- audit logging
- tool-result Artifacts

## Acceptance Criteria

- tools operate only inside Workspace;
- denied Capabilities never execute;
- path traversal is rejected;
- symlink escapes are rejected;
- commands run without sudo;
- destructive commands are denied;
- timeouts terminate processes;
- tool calls and outputs are persisted;
- output limits are enforced.

## Tests

- filesystem tool tests;
- command policy tests;
- timeout tests;
- Capability enforcement tests;
- path escape tests;
- audit tests;
- output truncation tests.

## Out of Scope

- remote shell;
- Docker;
- unrestricted network;
- Git push.

## Exit Criteria

Coding Agents have a safe, auditable local tool environment.

---

# Milestone 20 — Coding Role Runtime

## Goal

Implement Coding Role execution through Ollama and the safe tool runtime.

## Architecture References

- `docs/architecture/06_Roles.md`
- `docs/architecture/09_Workspace.md`
- `docs/architecture/17_Security.md`

## Dependencies

- Milestone 16
- Milestone 17
- Milestone 19

## Deliverables

- Coding prompt template
- Coding input builder
- tool-loop orchestrator
- max-step enforcement
- max-duration enforcement
- structured Coding result
- changed-file collection
- Coding RoleInvocation records
- Coding summary Artifact
- Git diff Artifact

## Acceptance Criteria

- Coding Agent operates only in assigned Workspace;
- Coding Agent receives exactly the admitted Capabilities;
- step and duration limits are enforced;
- changed files are observed independently;
- model claims are not trusted as verification;
- Coding output is schema-validated;
- Work Item cannot succeed based only on model response;
- prompts, tool calls, outputs, and diff are persisted.

## Tests

- simple file creation scenario;
- endpoint implementation fixture;
- max-step tests;
- invalid output tests;
- blocked-task tests;
- diff Artifact tests;
- Workspace isolation tests.

## Out of Scope

- verification verdict;
- review verdict;
- automatic commit;
- Git push.

## Exit Criteria

One Coding Work Item can be executed safely and produce inspectable Artifacts.

---

# Milestone 21 — Verification Controller

## Goal

Implement independent verification based on observed command results.

## Architecture References

- `docs/architecture/05_Workflows.md`
- `docs/architecture/07_Execution.md`
- `docs/architecture/17_Security.md`

## Dependencies

- Milestone 15
- Milestone 19
- Milestone 20

## Deliverables

- verification request model
- verification controller
- project-declared command execution
- exit-code collection
- stdout/stderr capture
- verification report Artifact
- verification Conditions
- retry routing
- failure categorization

## Acceptance Criteria

- verification commands come from approved Plan or Project configuration;
- test success uses observed exit code;
- model self-report is ignored;
- failed verification produces structured evidence;
- Execution routes to repair when allowed;
- command timeout is enforced;
- verification Artifacts are immutable.

## Tests

- successful verification;
- failed tests;
- timeout;
- missing command;
- repair routing;
- Artifact generation;
- restart recovery.

## Out of Scope

- Codex review;
- static-analysis plugin ecosystem;
- CI integration.

## Exit Criteria

Maestro independently determines whether implementation verification passed.

---

# Milestone 22 — Codex Reviewer Provider

## Goal

Implement Codex as the initial Reviewer Provider.

## Architecture References

- `docs/architecture/06_Roles.md`
- `docs/architecture/11_Providers.md`
- `docs/architecture/17_Security.md`

## Dependencies

- Milestone 10
- Milestone 12
- Milestone 13

## Deliverables

- Codex adapter
- non-interactive invocation
- read-only execution mode
- Reviewer prompt builder
- structured Review parser
- timeout handling
- normalized errors
- review input packaging
- mock Codex adapter for tests

## Acceptance Criteria

- Reviewer receives immutable Artifacts;
- Reviewer has no write Capabilities;
- structured Review validates;
- Codex failures produce normalized errors;
- missing evidence returns `UnableToReview`;
- prompts and outputs are persisted;
- Provider-specific code stays in infrastructure.

## Tests

- structured approve review;
- request-changes review;
- malformed output;
- timeout;
- missing Artifact;
- read-only-policy test.

## Out of Scope

- cloud fallback;
- reviewer ensembles;
- automatic fixes.

## Exit Criteria

Codex can produce structured Reviews through the generic Reviewer contract.

---

# Milestone 23 — Review and Repair Workflow

## Goal

Connect verification, Codex review, bounded repair, and final approval.

## Architecture References

- `docs/architecture/05_Workflows.md`
- `docs/architecture/13_State_Machine.md`
- `docs/architecture/17_Security.md`

## Dependencies

- Milestone 20
- Milestone 21
- Milestone 22

## Deliverables

- Review Work Item creation
- Review resource creation
- verdict routing
- request-changes routing
- repair Work Item creation
- iteration counters
- maximum repair enforcement
- final Approval resource
- NeedsHumanDecision handling
- terminal failure handling

## Acceptance Criteria

- `Approve` routes to final approval;
- `RequestChanges` creates one repair iteration;
- repair limits are enforced;
- `NeedsHumanDecision` pauses for human input;
- Reviewer never edits code;
- new diff and verification Artifacts are produced after repair;
- final approval references exact immutable subject versions;
- rejection does not silently merge or discard changes.

## Tests

- approve path;
- request-changes path;
- repair success;
- repair exhaustion;
- NeedsHumanDecision path;
- approval invalidation;
- restart during repair.

## Out of Scope

- parallel review;
- automatic merge;
- multiple reviewers.

## Exit Criteria

The complete coding, verification, review, repair, and final approval loop works without UI.

---

# Milestone 24 — REST API

## Goal

Expose MVP resources and user actions through a versioned REST API.

## Architecture References

- `docs/architecture/15_Web_API.md`
- `docs/development/API_STYLE_GUIDE.md`
- `docs/development/RESOURCE_SPECIFICATION.md`

## Dependencies

- Milestones 2–23

## Deliverables

- FastAPI application
- Project endpoints
- Execution endpoints
- Plan endpoints
- Work Item endpoints
- Artifact endpoints
- Review endpoints
- Approval actions
- Provider and Agent status endpoints
- pagination
- label filtering
- RFC 7807-style errors
- optimistic concurrency support
- OpenAPI documentation
- SSE Execution event stream

## Acceptance Criteria

- clients cannot write resource status;
- stale updates return conflict;
- approval actions bind exact subject versions;
- resource responses preserve metadata/spec/status;
- API contains no orchestration logic;
- errors are structured;
- Execution progress can be streamed;
- OpenAPI generation succeeds.

## Tests

- endpoint tests;
- validation tests;
- conflict tests;
- status-write-denial tests;
- approval tests;
- pagination tests;
- SSE tests;
- error contract tests.

## Out of Scope

- authentication;
- multi-user authorization;
- GraphQL;
- gRPC.

## Exit Criteria

The full MVP workflow can be driven through the API.

---

# Milestone 25 — Web UI

## Goal

Implement the minimal browser interface for one complete Execution.

## Architecture References

- `docs/architecture/16_Web_UI.md`
- `docs/development/UI_GUIDELINES.md`

## Dependencies

- Milestone 24

## Deliverables

- Project list
- Project detail
- new Execution form
- Plan view
- Plan approval
- Execution timeline
- Work Item status
- Agent invocation status
- Artifact browser
- diff viewer
- verification report
- Review findings
- final approval
- discard/cancel actions
- live SSE updates

## Acceptance Criteria

- user can create an Execution;
- user can inspect Goal and Plan;
- user can approve or reject Plan;
- current Workflow step is visible;
- Events and Artifacts are inspectable;
- diff and verification results are visible;
- Review findings are visible;
- user can make final approval decision;
- UI contains no orchestration logic;
- keyboard navigation works for primary flows.

## Tests

- template rendering;
- approval integration;
- live-update integration;
- primary browser-flow tests;
- basic accessibility tests;
- error-state tests.

## Out of Scope

- authentication;
- advanced dashboard;
- visual Workflow editor;
- mobile application;
- theme system.

## Exit Criteria

The complete MVP workflow can be operated from a browser.

---

# Milestone 26 — End-to-End MVP

## Goal

Validate Maestro's complete local-first vertical workflow.

## Architecture References

- all architecture documents;
- all applicable ADRs;
- `RESOURCE_SPECIFICATION.md`;
- `ARCHITECTURE_CHECKLIST.md`.

## Dependencies

- Milestones 0–25

## Demo Scenario

Use a small fixture repository.

Goal:

```text
Create a minimal FastAPI application.

Requirements:
- GET /health returns {"status":"ok"}
- Add one automated test
- Add README instructions
- Do not add a database
- Do not add authentication
## Deliverables

- end-to-end test harness
- fixture Git repository
- restart-recovery scenario
- failure scenarios
- demo instructions
- final architecture checklist
- MVP release notes
- implementation-plan status update

---

## Acceptance Criteria

- human creates Goal;
- Execution is persisted;
- Planner generates valid Plan;
- human approves Plan;
- Workspace is created;
- Coding Role modifies only Workspace;
- verification runs independently;
- Codex reviews immutable Artifacts;
- repair loop works when requested;
- human approves final result;
- Execution reaches Completed;
- every transition emits an Event;
- every important output becomes an Artifact;
- application restart does not lose Execution state;
- source checkout remains untouched;
- all tests, Ruff, and mypy pass;
- documentation is current.

---

## Required Failure Scenarios

- Ollama unavailable;
- Codex unavailable;
- invalid Planner output;
- Coding tool timeout;
- path traversal attempt;
- failed verification;
- Reviewer requests changes;
- repair limit exceeded;
- application restart while Execution is active;
- stale resourceVersion update.

---

## Out of Scope

- automatic Git push;
- automatic merge;
- remote workers;
- multiple concurrent Executions;
- cloud deployment;
- Kubernetes deployment.

---

## Exit Criteria

Maestro MVP is complete and ready for a `v0.1.0` local release.

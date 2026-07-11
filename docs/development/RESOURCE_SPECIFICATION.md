# Maestro Resource Specification

Version: 0.1  
API maturity: `v1alpha1`  
Status: Draft specification for MVP implementation

---

## 1. Purpose

This document defines the canonical resource model for Maestro.

It specifies:

- common resource structure;
- identity and metadata;
- desired state and observed state;
- validation rules;
- lifecycle and state transitions;
- Conditions;
- owner relationships;
- finalizers;
- optimistic concurrency;
- event emission;
- reconciliation expectations;
- examples for every first-class resource.

The specification is inspired by Kubernetes API conventions but adapted to Maestro's execution-centric domain.

This document is normative.

If prose elsewhere conflicts with this specification, this document takes precedence for resource shape and lifecycle semantics.

---

## 2. Design Goals

Maestro resources must be:

- durable;
- explicit;
- versioned;
- observable;
- auditable;
- declarative;
- safe to reconcile repeatedly;
- portable across local and distributed deployments;
- independent of any specific model provider;
- suitable for both API and persistence use.

The resource model must support the MVP while leaving room for:

- distributed workers;
- multiple repositories;
- pluggable Providers;
- remote Workspaces;
- multiple Knowledge Sources;
- multi-user authorization;
- custom Roles and Workflows;
- Kubernetes deployment.

---

## 3. Common Resource Envelope

Every first-class Maestro resource uses this shape:

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Execution
metadata:
  id: 7c9d2b2e-...
  name: add-health-endpoint
  namespace: default
  generation: 1
  resourceVersion: 4
  createdAt: 2026-07-11T12:00:00Z
  updatedAt: 2026-07-11T12:01:10Z
  createdBy: local-user
  labels: {}
  annotations: {}
  ownerReferences: []
  finalizers: []
spec: {}
status:
  observedGeneration: 1
  phase: Draft
  conditions: []
```

The common fields are:

| Field | Required | Meaning |
|---|---:|---|
| `apiVersion` | yes | Resource API version |
| `kind` | yes | Resource type |
| `metadata` | yes | Identity, versioning, ownership and auxiliary metadata |
| `spec` | yes | Desired state |
| `status` | yes | Observed state |

---

## 4. API Versioning

Initial API version:

```text
maestro.dev/v1alpha1
```

Version maturity follows:

```text
v1alpha1 → v1alpha2 → v1beta1 → v1
```

Rules:

- breaking schema changes require a new API version;
- resources must remain readable after upgrades;
- converters may migrate old versions;
- persisted resources record the version used at creation;
- Workflow and Role resources are also versioned independently inside their `spec`.

---

## 5. Resource Identity

### 5.1 `metadata.id`

A globally unique UUID.

Immutable.

Used for internal relationships.

### 5.2 `metadata.name`

Human-readable identifier.

Rules:

- lowercase;
- letters, numbers and hyphens;
- maximum 63 characters for MVP;
- unique within `kind + namespace`;
- immutable after creation in MVP.

Example:

```text
add-health-endpoint
```

### 5.3 `metadata.namespace`

Logical isolation boundary.

MVP default:

```text
default
```

Namespaces are reserved for future multi-user and multi-team installations.

### 5.4 Resource References

References use stable identity.

```yaml
projectRef:
  kind: Project
  id: 1db9...
  name: tour-manager
```

Minimum valid reference:

```yaml
id: UUID
kind: Project
```

Names are informative and may be omitted.

---

## 6. Metadata

### 6.1 Metadata Schema

```yaml
metadata:
  id: UUID
  name: string
  namespace: string
  generation: integer
  resourceVersion: integer
  createdAt: datetime
  updatedAt: datetime
  createdBy: string
  labels:
    string: string
  annotations:
    string: string
  ownerReferences:
    - apiVersion: string
      kind: string
      id: UUID
      name: string
      controller: boolean
      blockOwnerDeletion: boolean
  finalizers:
    - string
  deletionTimestamp: datetime | null
```

### 6.2 Generation

`generation` represents the revision of `spec`.

Rules:

- starts at `1`;
- increments only when `spec` changes;
- does not increment for status updates;
- immutable history may be preserved separately.

### 6.3 Resource Version

`resourceVersion` changes on every persisted mutation.

Used for optimistic concurrency.

Update request:

```yaml
metadata:
  resourceVersion: 7
```

If current version is `8`, the update fails with conflict.

### 6.4 Labels

Labels support selection and scheduling.

Recommended examples:

```yaml
labels:
  maestro.dev/project: tour-manager
  maestro.dev/domain: backend
  maestro.dev/priority: high
  maestro.dev/locality: desktop
```

Rules:

- searchable;
- short values;
- not used for large data;
- must not contain secrets.

### 6.5 Annotations

Annotations contain non-indexed auxiliary metadata.

Examples:

```yaml
annotations:
  maestro.dev/original-request-source: web-ui
  maestro.dev/prompt-sha256: 4f0c...
```

### 6.6 Owner References

Owner references express lifecycle ownership.

Example:

```yaml
ownerReferences:
  - apiVersion: maestro.dev/v1alpha1
    kind: Execution
    id: ...
    controller: true
    blockOwnerDeletion: true
```

MVP ownership expectations:

| Resource | Owner |
|---|---|
| Plan | Execution |
| WorkItem | Execution |
| Workspace | Execution |
| Artifact | Execution |
| Review | Execution |
| Approval | Execution |
| RoleInvocation | Execution |
| Event | Execution |
| Execution | Project |

### 6.7 Finalizers

Finalizers prevent deletion until cleanup completes.

Example:

```yaml
finalizers:
  - workspace.maestro.dev/cleanup
```

MVP finalizers:

```text
workspace.maestro.dev/cleanup
execution.maestro.dev/archive-artifacts
```

Hard deletion is discouraged.

Archival is preferred.

---

## 7. Spec and Status

### 7.1 Spec

`spec` describes desired state.

Sources of `spec` changes:

- human user;
- API client;
- controller creating subordinate resources;
- approved Workflow output.

Models may propose values, but models never write persisted `spec` directly without validation and controller mediation.

### 7.2 Status

`status` describes observed state.

Only Maestro controllers may write status.

Models must never directly set:

- `phase`;
- `conditions`;
- verification status;
- approval status;
- completion status.

### 7.3 Observed Generation

```yaml
status:
  observedGeneration: 3
```

A controller sets `observedGeneration` after reconciling the current `spec.generation`.

If:

```text
metadata.generation > status.observedGeneration
```

the resource has unapplied desired changes.

---

## 8. Conditions

Conditions communicate observed facts.

### 8.1 Condition Schema

```yaml
conditions:
  - type: Ready
    status: "True"
    reason: WorkspacePrepared
    message: Git worktree created successfully
    observedGeneration: 1
    lastTransitionTime: 2026-07-11T12:01:00Z
```

Fields:

| Field | Meaning |
|---|---|
| `type` | Stable machine-readable condition name |
| `status` | `"True"`, `"False"` or `"Unknown"` |
| `reason` | Stable machine-readable reason |
| `message` | Human-readable detail |
| `observedGeneration` | Spec generation evaluated |
| `lastTransitionTime` | Time condition status last changed |

### 8.2 Condition Rules

- one active Condition per type;
- update existing Condition instead of appending duplicates;
- `reason` uses PascalCase;
- `message` is not parsed by automation;
- conditions complement phase;
- unknown external state uses `"Unknown"`.

---

## 9. Phases and State Machines

`status.phase` is a concise state summary.

Phase rules:

- stable enum;
- controller-owned;
- transitions validated;
- terminal phases immutable without explicit retry/fork;
- every transition emits an Event.

Phase is not a substitute for detailed Conditions.

---

## 10. Deletion, Archival and Garbage Collection

### 10.1 Archival

Recommended for historical resources:

```yaml
spec:
  archived: true
```

or resource-specific archival command.

### 10.2 Deletion Flow

1. set `deletionTimestamp`;
2. stop new work;
3. controllers run finalizers;
4. remove finalizers;
5. delete resource;
6. garbage collect owned resources according to policy.

### 10.3 MVP Policy

- Projects: archive only by default;
- Executions: archive only by default;
- Workspaces: deletable after finalization;
- Events: immutable and retained with Execution history;
- Artifacts: retained unless explicit retention policy applies.

---

## 11. Project Resource

### 11.1 Purpose

A Project groups repositories, defaults, policies, Role bindings and Knowledge Source bindings.

### 11.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Project
metadata:
  name: tour-manager
spec:
  description: Application for touring musicians
  repositories:
    - id: backend
      path: /home/sashka/projects/tour-manager/backend
      defaultBranch: main
      type: git
    - id: frontend
      path: /home/sashka/projects/tour-manager/frontend
      defaultBranch: main
      type: git

  workflowRef:
    kind: Workflow
    name: software-delivery
    version: v1alpha1

  roleBindings:
    planner:
      agentRef:
        kind: Agent
        name: planner-local
    coding:
      agentRef:
        kind: Agent
        name: coder-local
    reviewer:
      agentRef:
        kind: Agent
        name: codex-reviewer

  knowledgeBindings:
    - kind: KnowledgeSource
      name: project-docs

  policies:
    requirePlanApproval: true
    requireFinalApproval: true
    allowNetwork: false
    allowDependencyChanges: approval-required

status:
  observedGeneration: 1
  phase: Ready
  repositories:
    - id: backend
      reachable: true
      gitRepository: true
      clean: true
      headRevision: abc123
  conditions:
    - type: Ready
      status: "True"
      reason: ConfigurationValidated
      observedGeneration: 1
```

### 11.3 Project Phases

```text
Pending
Validating
Ready
Degraded
Archived
Error
```

### 11.4 Validation

- repository IDs unique;
- repository paths absolute for local provider;
- Workflow reference exists;
- Role bindings resolve;
- Knowledge Source references resolve;
- no Project path may be nested inside Maestro data directories unless explicitly allowed.

### 11.5 Invariants

- Project does not own repository contents;
- deleting Project never deletes source repositories;
- historical Executions remain auditable after archival;
- Project configuration changes increment generation.

---

## 12. Execution Resource

### 12.1 Purpose

An Execution represents one complete orchestration attempt to satisfy one Goal.

It is Maestro's primary aggregate root.

### 12.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Execution
metadata:
  name: add-health-endpoint
  ownerReferences:
    - kind: Project
      id: ...
      controller: true
spec:
  projectRef:
    kind: Project
    id: ...

  goal:
    summary: Add a health endpoint
    description: |
      Create GET /health and return {"status":"ok"}.
    constraints:
      - Do not add a database
      - Do not modify authentication
    acceptanceCriteria:
      - GET /health returns HTTP 200
      - Response equals {"status":"ok"}
      - Automated tests pass

  workflowRef:
    kind: Workflow
    name: software-delivery
    version: v1alpha1

  policyRef:
    kind: Policy
    name: default-safe

  limits:
    maxCodingIterations: 2
    maxReviewIterations: 2
    maxDurationSeconds: 3600
    maxToolCallsPerInvocation: 40

  suspended: false
  cancellationRequested: false

status:
  observedGeneration: 1
  phase: Planning
  currentStep: planning
  approvedPlanRef: null
  activeWorkItemRefs: []
  workspaceRefs: []
  artifactRefs: []
  iteration:
    coding: 0
    review: 0
  startedAt: ...
  completedAt: null
  conditions:
    - type: GoalAccepted
      status: "True"
      reason: ValidationPassed
```

### 12.3 Execution Phases

```text
Draft
Planning
WaitingForUserInput
WaitingForPlanApproval
PreparingWorkspace
Executing
Verifying
Reviewing
WaitingForFinalApproval
Completed
Failed
Cancelled
Archived
```

### 12.4 Valid Transitions

```text
Draft → Planning
Planning → WaitingForUserInput
Planning → WaitingForPlanApproval
Planning → Failed
WaitingForUserInput → Planning
WaitingForUserInput → Cancelled
WaitingForPlanApproval → Planning
WaitingForPlanApproval → PreparingWorkspace
WaitingForPlanApproval → Cancelled
PreparingWorkspace → Executing
PreparingWorkspace → Failed
Executing → Verifying
Executing → Failed
Executing → Cancelled
Verifying → Reviewing
Verifying → Executing
Verifying → Failed
Reviewing → WaitingForFinalApproval
Reviewing → Executing
Reviewing → Failed
WaitingForFinalApproval → Completed
WaitingForFinalApproval → Executing
WaitingForFinalApproval → Cancelled
Completed → Archived
Failed → Archived
Cancelled → Archived
```

No implicit transition out of terminal phases.

### 12.5 Validation

- exactly one Goal;
- Goal summary non-empty;
- Workflow version must exist;
- limits finite and positive;
- Project must exist and be Ready or Degraded with compatible policy;
- Workflow must be valid;
- Goal cannot change after Planning starts except through a new Execution revision/fork.

### 12.6 Controller Responsibilities

ExecutionController:

- ensures planning Work Item exists;
- evaluates approvals;
- prepares Workspaces;
- schedules ready Work Items;
- advances phase based on persisted evidence;
- records terminal outcome;
- emits phase-change Events.

### 12.7 Invariants

- exactly one Goal;
- exactly one pinned Workflow version;
- status controller-owned;
- all subordinate resources owner-reference Execution;
- every phase transition emits an Event;
- terminal Executions do not resume implicitly.

---

## 13. Workflow Resource

### 13.1 Purpose

A Workflow is an immutable, versioned, declarative execution graph.

### 13.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Workflow
metadata:
  name: software-delivery
spec:
  version: v1alpha1
  description: Plan, code, verify, review and approve
  entrypoint: planning

  steps:
    - id: planning
      type: role
      roleRef:
        name: planner
        version: v1alpha1
      onSuccess: plan-approval

    - id: plan-approval
      type: approval
      subject: latestPlan
      onApproved: prepare-workspace
      onRejected: planning

    - id: prepare-workspace
      type: system
      controller: workspace
      onSuccess: execute-work-items

    - id: execute-work-items
      type: fanout
      source: approvedPlan.workItems
      maxParallel: 1
      onSuccess: verify

    - id: verify
      type: system
      controller: verification
      onSuccess: review
      onFailure: repair

    - id: review
      type: role
      roleRef:
        name: reviewer
        version: v1alpha1
      onApproved: final-approval
      onChangesRequested: repair

    - id: repair
      type: role
      roleRef:
        name: coding
        version: v1alpha1
      maxAttempts: 2
      onSuccess: verify
      onFailure: failed

    - id: final-approval
      type: approval
      subject: finalArtifacts
      onApproved: completed
      onRejected: cancelled

    - id: completed
      type: terminal
      outcome: success

    - id: failed
      type: terminal
      outcome: failure

    - id: cancelled
      type: terminal
      outcome: cancelled

status:
  observedGeneration: 1
  phase: Ready
  validation:
    valid: true
    errors: []
  conditions:
    - type: Ready
      status: "True"
      reason: GraphValidated
```

### 13.3 Workflow Phases

```text
Pending
Validating
Ready
Invalid
Deprecated
```

### 13.4 Validation

- immutable `spec.version`;
- unique step IDs;
- valid entrypoint;
- all transitions resolve;
- at least one terminal step;
- terminal paths reachable;
- Role references valid;
- retry bounds finite;
- expression language restricted;
- fanout source valid;
- no unbounded cycles.

### 13.5 Invariants

- Workflows are immutable by version;
- Executions pin a version;
- Workflows reference Roles, not Agents;
- no provider-specific fields;
- no direct filesystem or shell implementation details.

---

## 14. Plan Resource

### 14.1 Purpose

A Plan is a versioned proposal that decomposes a Goal into Work Items.

### 14.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Plan
metadata:
  name: execution-123-plan-1
  ownerReferences:
    - kind: Execution
      id: ...
      controller: true
spec:
  executionRef:
    kind: Execution
    id: ...
  version: 1
  summary: Bootstrap the application and add health endpoint
  assumptions:
    - Python 3.12 is available
  questions: []
  risks:
    - description: Repository is empty
      mitigation: Create only minimal structure
  workItems:
    - id: bootstrap
      title: Bootstrap backend
      roleRef:
        name: coding
        version: v1alpha1
      repositoryRef: backend
      objective: Create minimal project structure
      contextRefs: []
      constraints: []
      acceptanceCriteria:
        - Application imports
        - pytest runs
      verification:
        commands:
          - pytest
      dependsOn: []

status:
  observedGeneration: 1
  phase: WaitingForApproval
  validation:
    valid: true
    errors: []
  approvalRef: null
  conditions: []
```

### 14.3 Plan Phases

```text
Draft
Validating
WaitingForInput
WaitingForApproval
Approved
Rejected
Superseded
Invalid
```

### 14.4 Validation

- belongs to one Execution;
- version positive and unique per Execution;
- Work Item IDs unique within Plan;
- every Work Item has Role, objective and acceptance criteria;
- dependency graph acyclic;
- repository references valid;
- requested Capabilities compatible with Role.

### 14.5 Invariants

- immutable after creation;
- approved Plan cannot be modified;
- rejection creates new revision, not mutation;
- only one approved Plan per Execution;
- human approval required in MVP.

---

## 15. WorkItem Resource

### 15.1 Purpose

A WorkItem is the smallest schedulable unit of work.

### 15.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: WorkItem
metadata:
  name: implement-health
  labels:
    maestro.dev/role: coding
  ownerReferences:
    - kind: Execution
      id: ...
      controller: true
spec:
  executionRef:
    kind: Execution
    id: ...
  planRef:
    kind: Plan
    id: ...
  planWorkItemId: health-endpoint

  roleRef:
    name: coding
    version: v1alpha1

  repositoryRef: backend
  workspaceRef:
    kind: Workspace
    id: ...

  objective: Implement GET /health
  contextRefs:
    - kind: Artifact
      id: ...
  constraints:
    - Do not add unrelated dependencies
  acceptanceCriteria:
    - GET /health returns 200
    - Response equals {"status":"ok"}

  verification:
    commands:
      - pytest

  dependsOn:
    - kind: WorkItem
      id: ...

  requestedCapabilities:
    - filesystem.read
    - filesystem.write
    - filesystem.edit
    - shell.execute.test
    - git.diff

  retryPolicy:
    maxAttempts: 2

status:
  observedGeneration: 1
  phase: Ready
  assignedAgentRef: null
  invocationRefs: []
  attempt: 0
  resultArtifactRefs: []
  startedAt: null
  completedAt: null
  conditions:
    - type: DependenciesSatisfied
      status: "True"
      reason: AllDependenciesSucceeded
```

### 15.3 WorkItem Phases

```text
Pending
Blocked
Ready
Scheduled
Running
WaitingForTool
WaitingForApproval
Verifying
Reviewing
Succeeded
Failed
Cancelled
```

### 15.4 Readiness

WorkItem is Ready when:

- all dependencies succeeded;
- owning Execution active;
- Workspace Ready;
- Role exists;
- eligible Agent exists;
- required Capabilities can be granted;
- no approval gate blocks it.

### 15.5 Invariants

- belongs to exactly one Execution;
- belongs to exactly one Plan revision;
- references exactly one Role;
- success requires independent evidence;
- Agent cannot mutate phase;
- retries finite;
- dependencies acyclic.

---

## 16. Role Resource

### 16.1 Purpose

A Role defines a responsibility and policy contract.

### 16.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Role
metadata:
  name: coding
spec:
  version: v1alpha1
  purpose: Implement one software Work Item

  inputSchemaRef: CodingInput/v1
  outputSchemaRef: CodingResult/v1
  promptRef:
    kind: Artifact
    name: coding-prompt-v1

  requiredCapabilities:
    - filesystem.read
    - filesystem.write
    - filesystem.edit
    - git.status
    - git.diff

  optionalCapabilities:
    - shell.execute.test
    - shell.execute.build
    - knowledge.search

  prohibitedCapabilities:
    - git.push
    - deployment.execute
    - approval.decide
    - workflow.transition

  executionPolicy:
    maxSteps: 40
    maxDurationSeconds: 1800
    requireStructuredOutput: true
    requireIndependentVerification: true

status:
  observedGeneration: 1
  phase: Ready
  validation:
    valid: true
    errors: []
  conditions: []
```

### 16.3 Role Phases

```text
Pending
Validating
Ready
Invalid
Deprecated
```

### 16.4 Invariants

- Role does not name a model;
- immutable by version;
- no workflow transition capability;
- capability policy explicit;
- input and output schemas required;
- Workflow references Role version.

---

## 17. Agent Resource

### 17.1 Purpose

An Agent is a runtime configuration capable of fulfilling compatible Roles.

### 17.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Agent
metadata:
  name: coder-local
  labels:
    maestro.dev/locality: macbook
    maestro.dev/model-type: coding
spec:
  providerRef:
    kind: Provider
    name: ollama-local
  model: qwen2.5-coder:14b

  supportedRoles:
    - name: coding
      versions:
        - v1alpha1

  capabilityBindings:
    - kind: CapabilityBinding
      name: local-workspace-safe

  capacity:
    maxConcurrentAssignments: 1

  scheduling:
    priority: 100
    enabled: true

status:
  observedGeneration: 1
  phase: Ready
  currentAssignments: 0
  lastHeartbeatAt: ...
  modelAvailable: true
  conditions:
    - type: Ready
      status: "True"
      reason: ProviderAndModelReady
```

### 17.3 Agent Phases

```text
Pending
Ready
Busy
Degraded
Unavailable
Disabled
```

### 17.4 Invariants

- operational resource;
- scheduler selects Agent;
- no project memory stored inside Agent;
- Agent cannot self-grant Capabilities;
- Role compatibility required;
- Provider must be Ready.

---

## 18. Provider Resource

### 18.1 Purpose

A Provider connects Maestro to a model runtime.

### 18.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Provider
metadata:
  name: ollama-local
spec:
  type: ollama
  endpoint: http://127.0.0.1:11434
  authRef: null

  allowedModels:
    - qwen3:14b
    - qwen2.5-coder:14b

  dataPolicy:
    allowSourceCode: true
    allowSecrets: false
    allowPersonalData: false

  timeoutSeconds: 120

status:
  observedGeneration: 1
  phase: Ready
  capabilities:
    structuredOutput: true
    toolCalling: true
    streaming: true
  availableModels:
    - qwen3:14b
    - qwen2.5-coder:14b
  lastHealthCheckAt: ...
  conditions:
    - type: Ready
      status: "True"
      reason: HealthCheckPassed
```

### 18.3 Provider Phases

```text
Pending
Ready
Degraded
Unavailable
Disabled
```

### 18.4 Invariants

- Provider is infrastructure;
- domain logic never branches on provider type;
- credentials referenced, not embedded;
- health controller-owned;
- scheduler avoids incompatible data policies.

---

## 19. Capability Resource

### 19.1 Purpose

A Capability describes permission to perform an operation category.

### 19.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Capability
metadata:
  name: filesystem-write
spec:
  canonicalName: filesystem.write
  description: Write files inside an assigned Workspace
  sideEffectLevel: mutating
  approvalPolicy: none
  scopes:
    - workspace
  inputSchemaRef: FilesystemWriteInput/v1
  outputSchemaRef: FilesystemWriteResult/v1

status:
  observedGeneration: 1
  phase: Ready
  toolImplementations:
    - local-filesystem
  conditions: []
```

### 19.3 Side Effect Levels

```text
read-only
mutating
destructive
external
privileged
```

### 19.4 Invariants

- Capability is permission, not implementation;
- tools implement Capability;
- deny by default;
- destructive and privileged Capabilities require explicit policy;
- models cannot create Capabilities.

---

## 20. CapabilityBinding Resource

### 20.1 Purpose

Binds Capabilities to an Agent, Role, Project or Workspace policy.

### 20.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: CapabilityBinding
metadata:
  name: local-workspace-safe
spec:
  grants:
    - filesystem.read
    - filesystem.write
    - filesystem.edit
    - git.status
    - git.diff
    - shell.execute.test

  denies:
    - filesystem.read.outside-workspace
    - git.push
    - deployment.execute
    - secrets.read

  scopes:
    workspaceSelector:
      matchLabels:
        maestro.dev/type: git-worktree

status:
  observedGeneration: 1
  phase: Ready
  conditions: []
```

---

## 21. Workspace Resource

### 21.1 Purpose

A Workspace is an isolated execution environment.

### 21.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Workspace
metadata:
  name: execution-123-backend
  ownerReferences:
    - kind: Execution
      id: ...
      controller: true
  finalizers:
    - workspace.maestro.dev/cleanup

spec:
  executionRef:
    kind: Execution
    id: ...
  repositoryRef: backend
  providerRef:
    kind: WorkspaceProvider
    name: local-git-worktree

  baseRevision: main
  branchName: maestro/execution-123
  requestedPath: null

  policy:
    network: deny
    allowSecrets: false
    maxDiskBytes: 10737418240
    commandTimeoutSeconds: 300

status:
  observedGeneration: 1
  phase: Ready
  path: /var/lib/maestro/workspaces/execution-123/backend
  observedRevision: abc123
  dirty: false
  lockHolder: null
  conditions:
    - type: Ready
      status: "True"
      reason: WorktreeCreated
```

### 21.3 Workspace Phases

```text
Pending
Preparing
Ready
InUse
Dirty
Releasing
Released
Failed
```

### 21.4 Invariants

- all exposed paths remain inside boundary;
- source repository never modified directly;
- cleanup finalizer protects external state;
- Workspace attributable to Execution;
- symlink escape prevented;
- secret exposure denied by default.

---

## 22. Artifact Resource

### 22.1 Purpose

An Artifact is an immutable durable output.

### 22.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Artifact
metadata:
  name: execution-123-git-diff
  ownerReferences:
    - kind: Execution
      id: ...
      controller: true
spec:
  executionRef:
    kind: Execution
    id: ...
  workItemRef:
    kind: WorkItem
    id: ...

  type: git-diff
  mediaType: text/x-diff
  storage:
    uri: file:///var/lib/maestro/artifacts/...
  sha256: 8d1e...
  sizeBytes: 4821

  producer:
    subsystem: workspace-controller
    roleInvocationRef: null

  sourceRefs: []

status:
  observedGeneration: 1
  phase: Available
  verifiedSha256: 8d1e...
  conditions:
    - type: IntegrityVerified
      status: "True"
      reason: ChecksumMatched
```

### 22.3 Artifact Types

```text
goal
plan
prompt
model-response
tool-log
command-output
verification-report
git-diff
patch
review
summary
knowledge-result
```

### 22.4 Artifact Phases

```text
Pending
Available
Corrupt
Missing
Archived
```

### 22.5 Invariants

- immutable;
- checksummed;
- provenance required;
- external storage allowed;
- metadata always persisted;
- review references immutable Artifacts.

---

## 23. Review Resource

### 23.1 Purpose

A Review evaluates immutable Artifacts against explicit criteria.

### 23.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Review
metadata:
  name: execution-123-review-1
  ownerReferences:
    - kind: Execution
      id: ...
      controller: true
spec:
  executionRef:
    kind: Execution
    id: ...
  workItemRef:
    kind: WorkItem
    id: ...

  reviewerRoleRef:
    name: reviewer
    version: v1alpha1

  subjectRefs:
    - kind: Artifact
      id: ...
    - kind: Artifact
      id: ...

  acceptanceCriteria:
    - GET /health returns 200

  policy:
    requireTests: true
    securityChecks: true

status:
  observedGeneration: 1
  phase: Completed
  verdict: RequestChanges
  blockingFindings:
    - id: finding-1
      severity: high
      category: correctness
      file: app/main.py
      line: 14
      issue: Response body does not match the Goal
      evidence: Current response is {"ok":true}
      suggestedFix: Return {"status":"ok"}
  nonBlockingFindings: []
  missingEvidence: []
  completedAt: ...
  conditions: []
```

### 23.3 Review Phases

```text
Pending
Scheduled
Running
Completed
Failed
Cancelled
```

### 23.4 Verdicts

```text
Approve
RequestChanges
NeedsHumanDecision
UnableToReview
```

### 23.5 Invariants

- read-only Role in MVP;
- immutable subjects;
- structured findings;
- controller decides follow-up;
- Review cannot mutate Workspace;
- verdict based on recorded evidence.

---

## 24. Approval Resource

### 24.1 Purpose

An Approval records a human or policy decision over an immutable subject.

### 24.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Approval
metadata:
  name: execution-123-plan-approval
  ownerReferences:
    - kind: Execution
      id: ...
      controller: true
spec:
  executionRef:
    kind: Execution
    id: ...
  subjectRef:
    kind: Plan
    id: ...
    resourceVersion: 3

  type: plan
  requiredApprovers: 1
  expiresAt: null

status:
  observedGeneration: 1
  phase: Approved
  decisions:
    - actor: local-user
      decision: approve
      comment: Proceed
      decidedAt: ...
      requestSource: web-ui
  conditions: []
```

### 24.3 Approval Phases

```text
Pending
Approved
Rejected
Expired
Invalidated
Cancelled
```

### 24.4 Invalidation

Approval invalidates when:

- subject generation changes;
- referenced resource deleted;
- policy changes materially;
- explicit revocation occurs.

### 24.5 Invariants

- attributable actor;
- immutable decision history;
- exact subject resource version;
- models cannot impersonate human approvers;
- approval does not directly mutate target resource.

---

## 25. Event Resource

### 25.1 Purpose

An Event is an immutable statement that something occurred.

### 25.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Event
metadata:
  name: event-01j...
  ownerReferences:
    - kind: Execution
      id: ...
spec:
  sequence: 42
  type: WorkItemSucceeded
  occurredAt: 2026-07-11T12:03:00Z
  producer: work-item-controller
  correlationId: ...
  executionRef:
    kind: Execution
    id: ...
  subjectRef:
    kind: WorkItem
    id: ...
  payload:
    resultArtifactRefs:
      - kind: Artifact
        id: ...

status:
  observedGeneration: 1
  phase: Recorded
  conditions: []
```

### 25.3 Event Invariants

- append-only;
- immutable;
- ordered within Execution by sequence;
- duplicate delivery tolerated;
- resources remain current-state authority;
- Event creation does not bypass resource reconciliation.

---

## 26. RoleInvocation Resource

### 26.1 Purpose

A RoleInvocation records one Agent attempt to fulfill one Role.

### 26.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: RoleInvocation
metadata:
  name: invocation-123
  ownerReferences:
    - kind: Execution
      id: ...
      controller: true
spec:
  executionRef:
    kind: Execution
    id: ...
  workItemRef:
    kind: WorkItem
    id: ...
  roleRef:
    name: coding
    version: v1alpha1
  agentRef:
    kind: Agent
    name: coder-local

  inputArtifactRefs: []
  grantedCapabilities:
    - filesystem.read
    - filesystem.write
    - shell.execute.test

  limits:
    maxSteps: 40
    maxDurationSeconds: 1800

status:
  observedGeneration: 1
  phase: Succeeded
  providerRef:
    kind: Provider
    name: ollama-local
  model: qwen2.5-coder:14b
  promptArtifactRef:
    kind: Artifact
    id: ...
  responseArtifactRef:
    kind: Artifact
    id: ...
  toolCallCount: 12
  startedAt: ...
  completedAt: ...
  failure: null
  conditions: []
```

### 26.3 Invocation Phases

```text
Pending
Assigned
Running
WaitingForTool
Succeeded
Failed
Cancelled
TimedOut
```

### 26.4 Invariants

- exact Role, Agent, Provider and model recorded;
- granted Capabilities persisted;
- input and output schema validated;
- model response not authoritative;
- one invocation belongs to one WorkItem.

---

## 27. KnowledgeSource Resource

### 27.1 Purpose

A KnowledgeSource describes a searchable source of contextual information.

### 27.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: KnowledgeSource
metadata:
  name: project-docs
spec:
  type: filesystem
  configuration:
    rootPath: /mnt/nas/knowledge/tour-manager
    readOnly: true
    include:
      - "**/*.md"
      - "**/*.pdf"
    exclude:
      - "**/.git/**"

  accessPolicy:
    projectRefs:
      - kind: Project
        id: ...

  indexing:
    mode: on-demand
    embeddingProviderRef: null

status:
  observedGeneration: 1
  phase: Ready
  documentCount: 42
  lastIndexedAt: ...
  conditions:
    - type: Ready
      status: "True"
      reason: SourceReachable
```

### 27.3 KnowledgeSource Phases

```text
Pending
Indexing
Ready
Degraded
Unavailable
Disabled
```

### 27.4 Invariants

- read-only by default;
- Project-scoped access;
- source provenance retained;
- retrieved content treated as untrusted data;
- credentials referenced, not embedded.

---

## 28. KnowledgeResult Artifact

Knowledge retrieval produces an Artifact.

```yaml
spec:
  type: knowledge-result
  producer:
    subsystem: knowledge-controller
  sourceRefs:
    - kind: KnowledgeSource
      id: ...
  content:
    query: authentication architecture
    results:
      - documentRef: docs/auth.md
        excerpt: ...
        startLine: 10
        endLine: 42
        score: 0.91
        checksum: ...
```

This makes context explicit and reproducible.

---

## 29. Policy Resource

### 29.1 Purpose

A Policy defines constraints for Projects, Workflows, Roles or Capabilities.

### 29.2 Schema

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Policy
metadata:
  name: default-safe
spec:
  approvals:
    requirePlanApproval: true
    requireFinalApproval: true
    dependencyChanges: approval-required

  capabilities:
    default: deny
    grants:
      - filesystem.read
      - git.diff
    denies:
      - git.push
      - deployment.execute
      - secrets.read

  workspace:
    allowNetwork: false
    allowSecrets: false
    maxCommandSeconds: 300

  providers:
    allowCloud: false

status:
  observedGeneration: 1
  phase: Ready
  validation:
    valid: true
    errors: []
  conditions: []
```

### 29.3 Invariants

- deny overrides grant;
- Policy cannot weaken higher-scope mandatory deny;
- changes increment generation;
- active Executions remain pinned to policy snapshot unless explicitly reconciled.

---

## 30. Worker Resource

Worker is optional for MVP persistence but reserved for remote execution.

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Worker
metadata:
  name: ubuntu-desktop
  labels:
    maestro.dev/os: linux
    maestro.dev/gpu: nvidia
spec:
  endpoint: https://...
  supportedWorkspaceProviders:
    - git-worktree
  capacity:
    maxConcurrentAssignments: 2

status:
  observedGeneration: 1
  phase: Ready
  activeAssignments: 0
  lastHeartbeatAt: ...
  conditions: []
```

---

## 31. Common Validation Rules

All resources must validate:

- valid API version;
- expected kind;
- required metadata;
- non-negative generation;
- positive resourceVersion;
- valid references;
- no unknown enum values;
- no duplicate list identities;
- no secrets in annotations or labels;
- finite retry and duration limits;
- immutable fields unchanged on update.

---

## 32. Mutability Matrix

| Resource | Spec mutable? | Status mutable? | Versioned? |
|---|---:|---:|---:|
| Project | yes | yes | generation |
| Execution | limited | yes | generation |
| Workflow | no after creation | yes | explicit version |
| Plan | no | yes | explicit version |
| WorkItem | limited before Running | yes | generation |
| Role | no after creation | yes | explicit version |
| Agent | yes | yes | generation |
| Provider | yes | yes | generation |
| Capability | rarely | yes | generation |
| Workspace | limited | yes | generation |
| Artifact | no | yes | immutable |
| Review | no | yes | immutable subject |
| Approval | no | yes | immutable decision history |
| Event | no | no except record status | immutable |
| RoleInvocation | no after start | yes | immutable input |
| KnowledgeSource | yes | yes | generation |
| Policy | yes | yes | generation |

---

## 33. Status Write Ownership

| Resource | Status writer |
|---|---|
| Project | ProjectController |
| Execution | ExecutionController |
| Workflow | WorkflowController |
| Plan | PlanController |
| WorkItem | WorkItemController |
| Role | RoleController |
| Agent | AgentController |
| Provider | ProviderController |
| Capability | CapabilityController |
| Workspace | WorkspaceController |
| Artifact | ArtifactController |
| Review | ReviewController |
| Approval | ApprovalController |
| Event | EventStore |
| RoleInvocation | InvocationController |
| KnowledgeSource | KnowledgeController |
| Policy | PolicyController |

No model writes status.

---

## 34. Reconciliation Requirements

Every controller must:

- be idempotent;
- use optimistic concurrency;
- tolerate duplicate Events;
- recover after restart;
- avoid duplicate subordinate resources;
- emit Events for meaningful transitions;
- update Conditions consistently;
- set `observedGeneration`;
- preserve failure evidence;
- never silently relax policy.

Controller pseudocode:

```python
async def reconcile(resource_id: UUID) -> None:
    resource = await repository.get(resource_id)

    desired = resource.spec
    observed = resource.status

    action = compute_reconciliation_action(desired, observed)

    if action is None:
        return

    result = await execute_action(action)
    await persist_status_with_resource_version(resource, result)
    await append_event(result.event)
```

---

## 35. API Semantics

### 35.1 Create

```text
POST /api/v1/<resources>
```

Server assigns:

- ID;
- generation;
- resourceVersion;
- timestamps.

### 35.2 Read

```text
GET /api/v1/<resources>/<id>
```

### 35.3 List

```text
GET /api/v1/<resources>?labelSelector=...
```

### 35.4 Update Spec

```text
PUT /api/v1/<resources>/<id>
```

Requires current `resourceVersion`.

### 35.5 Patch Spec

```text
PATCH /api/v1/<resources>/<id>
```

MVP may support JSON Merge Patch only.

### 35.6 Status Update

Internal controller endpoint or repository method only.

External clients cannot write status.

### 35.7 Delete

```text
DELETE /api/v1/<resources>/<id>
```

Sets deletion timestamp and begins finalization.

---

## 36. Persistence Mapping

The logical resource envelope should remain independent of database structure.

MVP may store:

- common metadata columns;
- JSON `spec`;
- JSON `status`;
- indexed foreign keys;
- normalized Event table;
- Artifact metadata table.

Recommended common columns:

```text
id
api_version
kind
namespace
name
generation
resource_version
created_at
updated_at
created_by
labels_json
annotations_json
spec_json
status_json
deletion_timestamp
```

High-value relationships may use explicit foreign keys.

---

## 37. Event Emission Matrix

Examples:

| Change | Event |
|---|---|
| Execution created | `ExecutionCreated` |
| Execution phase changed | `ExecutionPhaseChanged` |
| Plan created | `PlanCreated` |
| Plan approved | `PlanApproved` |
| WorkItem scheduled | `WorkItemScheduled` |
| Role invocation started | `RoleInvocationStarted` |
| Tool denied | `CapabilityDenied` |
| Workspace ready | `WorkspaceReady` |
| Verification completed | `VerificationCompleted` |
| Review completed | `ReviewCompleted` |
| Approval decided | `ApprovalDecided` |
| Execution completed | `ExecutionCompleted` |

---

## 38. Error Representation

Status failure:

```yaml
status:
  phase: Failed
  failure:
    code: ProviderUnavailable
    message: Ollama endpoint did not respond
    retryable: true
    occurredAt: ...
    details:
      providerRef: ollama-local
```

Error codes must be stable.

Suggested categories:

```text
ValidationFailed
Conflict
NotFound
PolicyDenied
ProviderUnavailable
ModelOutputInvalid
ToolFailed
CommandTimedOut
VerificationFailed
WorkspaceFailed
ArtifactCorrupt
ApprovalRejected
Cancelled
InternalError
```

---

## 39. Retention

MVP defaults:

| Resource | Retention |
|---|---|
| Projects | indefinite |
| Executions | indefinite |
| Plans | indefinite |
| WorkItems | indefinite |
| Events | indefinite |
| Reviews | indefinite |
| Approvals | indefinite |
| RoleInvocations | indefinite |
| Workspaces | until acceptance or cleanup |
| Large command output | configurable |
| Artifacts | indefinite for MVP |

Future retention policy may archive to NAS or object storage.

---

## 40. Security Requirements

- external clients cannot write status;
- models cannot directly persist resources;
- secrets referenced, never embedded;
- path and command policies enforced before tool execution;
- cross-Project references rejected by default;
- Approval subject resourceVersion required;
- Artifact integrity verified;
- audit history append-only;
- Provider data policy checked by scheduler.

---

## 41. MVP Resource Set

Required for MVP implementation:

```text
Project
Execution
Workflow
Plan
WorkItem
Role
Agent
Provider
Capability
CapabilityBinding
Workspace
Artifact
Review
Approval
Event
RoleInvocation
Policy
```

Optional but recommended after the first vertical slice:

```text
KnowledgeSource
Worker
WorkspaceProvider
```

---

## 42. Implementation Order

1. Common resource envelope
2. Project
3. Execution
4. Workflow
5. Plan
6. WorkItem
7. Role
8. Agent
9. Provider
10. Capability and CapabilityBinding
11. Workspace
12. Artifact
13. Approval
14. RoleInvocation
15. Review
16. Event
17. Policy
18. KnowledgeSource
19. Worker

Every resource should include:

- Pydantic model;
- enum definitions;
- validation;
- persistence repository;
- API schema;
- unit tests;
- lifecycle tests.

---

## 43. Resource Test Template

For every resource test:

```gherkin
Given a valid resource specification
When the resource is created
Then generation is 1
And resourceVersion is assigned
And status has its initial phase
```

```gherkin
Given an existing resource
When spec is updated with the current resourceVersion
Then generation increments
And resourceVersion increments
```

```gherkin
Given an existing resource
When status is updated by its controller
Then generation does not change
And resourceVersion increments
```

```gherkin
Given a stale resourceVersion
When an update is attempted
Then a Conflict error is returned
```

Also test:

- validation failures;
- immutable fields;
- deletion/finalizers;
- invalid state transitions;
- cross-resource references;
- Conditions;
- archival.

---

## 44. Global Invariants

```yaml
invariants:
  - Every first-class resource uses apiVersion, kind, metadata, spec and status
  - Spec represents desired state
  - Status represents observed state
  - Only controllers write status
  - Models never directly persist resources
  - Generation changes only when spec changes
  - ResourceVersion changes on every mutation
  - Every status transition emits an Event
  - Events are immutable
  - Artifacts are immutable
  - Plans are immutable by version
  - Workflows are immutable by version
  - Roles are immutable by version
  - Executions pin Workflow and Role versions
  - Every subordinate Execution resource has an owner reference
  - Finalizers protect external cleanup
  - Optimistic concurrency is mandatory
  - Deny-by-default security applies to Capabilities
  - Terminal resources do not resume implicitly
```

---

## 45. Design Decisions

- Kubernetes-like resource envelopes are used consistently.
- Desired and observed state are strictly separated.
- Execution is the primary aggregate.
- Resource status is controller-owned.
- Plans, Roles, Workflows, Events and Artifacts are immutable by version.
- Capabilities are permissions, not tools.
- Approval is a first-class resource.
- Reconciliation is idempotent.
- Optimistic concurrency is required from MVP.
- Archival is preferred over hard deletion.

---

## 46. Open Questions

These questions may be deferred until implementation exposes concrete needs:

- Should `spec` and `status` be stored as JSON or normalized tables?
- Should Workflow definitions be editable through the UI in MVP?
- Should Policy be fully generic or use typed substructures?
- Should Review remain separate from WorkItem long-term?
- Should Approval be a generic resource or Execution subresource?
- Should namespace be exposed in MVP UI?
- Which resources need formal finalizers in v0.1?
- Should owner-reference garbage collection run immediately or asynchronously?
- Should resource names be immutable forever?
- Should server-side apply be supported after v1?

When implementation reaches one of these questions, create an ADR before changing the specification.

---

## 47. Future Evolution

- namespaced multi-user resources;
- custom Role definitions;
- custom Workflow definitions;
- policy admission webhooks;
- signed resources;
- server-side apply;
- resource watches;
- distributed controller leaders;
- garbage collection controller;
- Kubernetes CRD projection;
- cross-cluster Workers;
- resource federation;
- formal schema registry;
- JSON Schema publication;
- OpenAPI generation;
- CLI resource manifests;
- declarative GitOps operation.

---

## 48. Summary

Maestro resources are the language of the control plane.

Humans declare desired state.

Models produce structured proposals and execution results.

Controllers validate and reconcile.

Status records observed reality.

Events explain history.

Artifacts preserve evidence.

The resource model must remain stable, explicit and auditable because every API endpoint, controller, UI view, policy decision and persistence operation depends on it.

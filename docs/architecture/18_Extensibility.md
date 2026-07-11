# Extensibility

Version: 0.1

## Purpose

Maestro must be extensible without turning the core into a collection of provider-specific branches.

Extension points are explicit architectural contracts.

The core domain remains independent from implementations.

## Extension Philosophy

```text
Core domain
    ↓
Stable interfaces
    ↓
Adapters and plugins
```

Extensions may add:

- Providers;
- Roles;
- Capabilities;
- Tools;
- Knowledge Sources;
- Workspace backends;
- Workflow definitions;
- notification systems;
- persistence backends;
- policy evaluators.

## Plugin Categories

### Provider Plugin

Connects Maestro to a model runtime.

Examples:

- Ollama;
- OpenAI;
- Anthropic;
- vLLM;
- LM Studio.

### Capability Plugin

Implements one or more Capabilities.

Examples:

- local filesystem;
- SSH filesystem;
- local shell;
- remote shell;
- GitHub PR creation.

### Knowledge Provider Plugin

Searches and retrieves context.

Examples:

- filesystem;
- NAS;
- Git;
- Odysseus Documents;
- Confluence;
- Obsidian.

### Workspace Provider Plugin

Creates isolated execution environments.

Examples:

- Git worktree;
- Docker;
- Podman;
- VM;
- remote SSH worker.

### Persistence Plugin

Stores resources and Artifacts.

Examples:

- SQLite;
- PostgreSQL;
- local object storage;
- S3-compatible storage.

### Notification Plugin

Sends updates.

Examples:

- Telegram;
- email;
- Slack;
- webhooks.

## Interface Stability

Interfaces are versioned.

```text
Provider/v1alpha1
KnowledgeProvider/v1alpha1
WorkspaceProvider/v1alpha1
```

Breaking changes require a new interface version.

## Provider Interface

Conceptual contract:

```python
class ModelProvider(Protocol):
    async def health(self) -> ProviderHealth: ...
    async def list_models(self) -> list[ModelInfo]: ...
    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
    ) -> StructuredGenerationResult: ...
    async def run_tool_loop(
        self,
        request: ToolLoopRequest,
    ) -> ToolLoopResult: ...
```

Not every Provider must implement every operation.

Capabilities are advertised through Provider status.

## Knowledge Provider Interface

```python
class KnowledgeProvider(Protocol):
    async def search(
        self,
        query: KnowledgeQuery,
    ) -> list[KnowledgeResult]: ...

    async def fetch(
        self,
        ref: KnowledgeReference,
    ) -> KnowledgeDocument: ...
```

## Workspace Provider Interface

```python
class WorkspaceProvider(Protocol):
    async def prepare(
        self,
        request: WorkspaceRequest,
    ) -> WorkspaceHandle: ...

    async def execute(
        self,
        handle: WorkspaceHandle,
        command: CommandRequest,
    ) -> CommandResult: ...

    async def collect_artifacts(
        self,
        handle: WorkspaceHandle,
    ) -> list[ArtifactDescriptor]: ...

    async def cleanup(
        self,
        handle: WorkspaceHandle,
    ) -> None: ...
```

## Role Packages

A Role package may contain:

```text
role.yaml
input.schema.json
output.schema.json
prompt.md
evals/
examples/
README.md
```

Example:

```yaml
apiVersion: maestro.dev/v1alpha1
kind: Role
metadata:
  name: security-reviewer
spec:
  version: v1
  inputSchemaRef: input.schema.json
  outputSchemaRef: output.schema.json
  promptRef: prompt.md
```

## Workflow Packages

A Workflow package may contain:

```text
workflow.yaml
schemas/
policies/
tests/
README.md
```

Workflow validation occurs before registration.

## Tool Registration

A tool declares:

- implemented Capabilities;
- input schema;
- output schema;
- side-effect level;
- required approvals;
- supported Workspace types;
- timeout policy.

```yaml
name: local-filesystem
implements:
  - filesystem.read
  - filesystem.write
sideEffects:
  filesystem.write: mutating
```

## Plugin Discovery

MVP recommendation:

- explicit Python configuration;
- no dynamic third-party plugin loading.

Later options:

- Python entry points;
- signed plugin bundles;
- containerized plugins;
- remote gRPC plugins.

## Compatibility

An extension declares:

```yaml
compatibility:
  maestroApi:
    min: v1alpha1
    max: v1alpha2
  platforms:
    - linux
    - macos
```

## Isolation

Extensions should not receive unrestricted process access by default.

Future plugin isolation may use:

- subprocess boundaries;
- containers;
- gRPC;
- WebAssembly;
- signed packages.

## Configuration

Extension configuration must be separated from secrets.

```yaml
spec:
  endpoint: http://localhost:11434
  authRef: ollama-auth
```

## Extension Testing

Every extension should provide:

- contract tests;
- health tests;
- failure-mode tests;
- timeout tests;
- schema validation tests;
- compatibility metadata.

## Core Versus Extension Boundary

Core includes:

- resource model;
- Execution lifecycle;
- Workflow reconciliation;
- scheduling contracts;
- policy enforcement;
- persistence interfaces;
- event semantics.

Extensions include:

- provider-specific API calls;
- external storage details;
- specific model integrations;
- NAS implementations;
- notification channels;
- remote execution transports.

## Anti-Patterns

Avoid:

- provider checks in domain code;
- special cases based on model name;
- plugin-specific database columns in core tables;
- arbitrary code execution during plugin discovery;
- hidden global state;
- tools granting Capabilities themselves.

## Extensibility Invariants

```yaml
invariants:
  - Core domain does not import provider implementations
  - Plugins implement versioned interfaces
  - Extensions declare capabilities
  - Tools do not grant permissions
  - Plugin failures do not corrupt Execution state
  - Extension configuration references secrets
  - Workflow and Role packages are validated before use
```

## Design Decisions

- The MVP uses explicit built-in adapters.
- Dynamic plugin loading is deferred.
- Interfaces are versioned from the beginning.
- Roles and Workflows are packageable resources.
- Capabilities form the compatibility layer between Roles and Tools.

## Open Questions

- Should third-party plugins run in-process?
- Should plugins be distributed as Python packages or containers?
- How should plugin signatures be verified?
- Should Role packages support inheritance?
- Should remote plugins use gRPC or MCP?
- How should plugin compatibility be negotiated?

## Future Evolution

- Signed plugin registry.
- Python entry-point discovery.
- Containerized extensions.
- Remote plugin protocol.
- Plugin health dashboards.
- Automatic compatibility checks.
- Community Role and Workflow catalogs.
- Policy-controlled plugin admission.

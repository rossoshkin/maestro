# Event System

Version: 0.1

## Purpose

Events are immutable facts describing something that has already happened.

Resources hold current state.

Events explain how the system reached that state.

## Philosophy

Maestro is event-driven but state-authoritative.

Events trigger reconciliation.

Persisted resources remain the source of truth.

## Event Envelope

```yaml
id: UUID
type: WorkItemCompleted
occurredAt: ...
producer: work-item-controller
executionRef: ...
payload: {}
```

## Categories

- Lifecycle
- Workflow
- Role Invocation
- Workspace
- Verification
- Review
- System

## Guarantees

- Immutable
- Ordered within an Execution
- Durable
- Replayable

## Event Bus

Publish → Persist → Notify Controllers

Controllers must tolerate duplicate delivery.

## Design Decisions

Events are append-only.

## Future

Distributed event streaming, subscriptions, webhooks.

# Execution

Version: 0.1

## Purpose

Execution is the primary aggregate of Maestro. Every Goal creates exactly one Execution.

## Responsibilities

- Own Goal lifecycle
- Pin Workflow version
- Own Plans, Work Items and Artifacts
- Persist current state
- Expose progress
- Coordinate reconciliation

## Lifecycle

Draft → Planning → WaitingForApproval → Executing → Verifying → Reviewing → Completed

## Execution Controller

Responsibilities:

- reconcile desired state
- create missing resources
- emit Events
- update Status

Controllers are idempotent.

## Checkpoints

Execution state is checkpointed after every successful transition.

## Recovery

After restart, controllers reload persisted Executions and continue reconciliation.

## Invariants

- Exactly one Goal
- Exactly one active Workflow version
- Status written only by controllers
- Every transition emits an Event

## Design Decisions

Execution is the system's aggregate root.

## Future Evolution

Execution replay, forks, distributed execution.

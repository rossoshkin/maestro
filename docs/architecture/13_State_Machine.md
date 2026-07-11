# State Machine

Version: 0.1

## Purpose

Every resource owns an explicit state machine.

## Execution

Draft
→ Planning
→ WaitingForApproval
→ Executing
→ Verifying
→ Reviewing
→ Completed

## Work Item

Pending
→ Ready
→ Running
→ Verifying
→ Succeeded

## Rules

Transitions only occur after persisted evidence.

Controllers own transitions.

Models never mutate state directly.

## Validation

Illegal transitions are rejected.

## Future

Visual state editor and formal verification.

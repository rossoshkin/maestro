# Workspace

Version: 0.1

Workspace is an isolated execution environment.

## Goals

- Isolation
- Reproducibility
- Safety

## MVP

Git worktree per Execution.

## Responsibilities

- prepare repository
- execute commands
- collect artifacts
- enforce filesystem boundaries

## Layout

Execution
 └── Workspace
      ├── backend
      └── frontend

## Policies

- No path escape
- Optional network
- Secret isolation

## Invariants

Workspace never modifies the source repository directly.

## Future

Containers, VMs, remote workers.

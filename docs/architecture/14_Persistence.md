# Persistence

Version: 0.1

## Goals

- Durable
- Auditable
- Recoverable

## Stored Resources

- Projects
- Executions
- Plans
- Work Items
- Events
- Artifacts
- Reviews
- Role Invocations

## MVP

SQLite

Future:

- PostgreSQL
- Object storage
- Event archive

## Concurrency

Optimistic concurrency using resourceVersion.

## Recovery

Controllers rebuild runtime state from persisted resources.

## Future

Snapshots, archival, replication.

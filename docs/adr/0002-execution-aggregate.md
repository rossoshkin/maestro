# ADR 0002 — Execution Aggregate

## Status
Accepted

## Context
Execution is the aggregate root. Plans, Artifacts, Reviews and Events belong to an Execution because users care about completing Goals, not isolated workflow steps.

## Decision
Execution is the aggregate root. Plans, Artifacts, Reviews and Events belong to an Execution because users care about completing Goals, not isolated workflow steps.

## Consequences
Improves modularity, portability and long-term maintainability.

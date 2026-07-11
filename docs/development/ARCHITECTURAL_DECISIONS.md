# Architectural Decisions

## Purpose
This document explains *why* Maestro is designed as documented.

## Core decisions
1. Execution is the aggregate root.
2. Workflows are declarative.
3. Controllers reconcile resources.
4. Models never mutate resources.
5. Roles define responsibilities; Agents execute Roles.
6. Providers are infrastructure adapters.
7. Capabilities authorize actions; tools implement capabilities.
8. Events and Artifacts are immutable.
9. Human approval is a first-class resource.
10. Architecture changes require ADRs before implementation.

## Rule
Implementation may extend the system but must never silently redefine these decisions.

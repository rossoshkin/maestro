# Roadmap

Version: 0.1

## Purpose

This roadmap translates Maestro's architecture into incremental delivery milestones.

The roadmap prioritizes one reliable vertical workflow over platform breadth.

## Product Principle

Do not implement the entire platform before validating the core loop.

The first success criterion is:

```text
Human Goal
  ↓
Planner Role
  ↓
Human Plan Approval
  ↓
Coding Role
  ↓
Independent Verification
  ↓
Codex Reviewer
  ↓
Human Final Approval
```

## Version 0.1 — MVP

### Goal

Deliver one complete local software-delivery Execution.

### Scope

- one local user;
- one Project;
- one repository per Execution;
- one Workflow;
- Planner via Ollama;
- Coding Role via Ollama;
- Reviewer via Codex;
- SQLite;
- Git worktree Workspace;
- basic web UI;
- plan approval;
- final approval;
- bounded repair loop;
- Execution timeline;
- Artifact viewer;
- Git diff viewer.

### Exit Criteria

- complete FastAPI health-endpoint scenario succeeds repeatedly;
- Execution survives application restart;
- Coding changes occur only inside the worktree;
- test results are independently observed;
- Codex returns structured review;
- user can approve or discard final changes;
- all prompts, invocations, tool calls, Events, and Artifacts are auditable.

## Version 0.2 — Specialized Roles

### Scope

- Frontend Developer Role;
- Backend Developer Role;
- Research Role;
- Documentation Role;
- multiple repositories per Project;
- Work Item dependency graph;
- sequential multi-Role Execution;
- richer Project instructions.

### Exit Criteria

- Planner assigns frontend and backend Work Items correctly;
- repository context is isolated by Work Item;
- combined final review includes all repository Artifacts.

## Version 0.3 — Knowledge

### Scope

- filesystem Knowledge Provider;
- Markdown and text indexing;
- Project knowledge bindings;
- Knowledge Result Artifacts;
- Git documentation source;
- NAS-backed read-only source;
- retrieval audit trail.

### Exit Criteria

- Planner and Reviewer can retrieve project documentation;
- every excerpt is attributable to a source and location;
- Knowledge Source access respects Project policy.

## Version 0.4 — Remote Workers

### Scope

- Ubuntu worker;
- remote Workspace execution;
- remote Ollama provider;
- worker registration;
- health and capacity;
- secure transport;
- Artifact transfer.

### Exit Criteria

- Maestro control plane on Mac schedules coding work on Ubuntu;
- code and tests run in the real remote repository Workspace;
- worker outage does not corrupt Execution state.

## Version 0.5 — Git Collaboration

### Scope

- commit creation;
- GitHub and GitLab adapters;
- pull request creation;
- branch policies;
- review comments;
- patch export;
- merge approval gate.

### Exit Criteria

- approved Execution can create a reviewable PR;
- Maestro never merges without explicit policy or approval;
- all remote Git operations are attributable.

## Version 0.6 — Notifications and Remote Control

### Scope

- Telegram;
- email;
- Slack;
- webhook events;
- approval links;
- mobile-friendly Execution view.

### Exit Criteria

- user receives completion and approval notifications;
- remote approval is authenticated and bound to an immutable subject.

## Version 0.7 — Parallelism

### Scope

- parallel independent Work Items;
- Agent pools;
- scheduler capacity;
- joins;
- conflict detection;
- multiple Coding Agents.

### Exit Criteria

- independent Work Items execute concurrently;
- conflicting repository changes are detected before integration;
- scheduler respects limits and locality.

## Version 0.8 — Policy Platform

### Scope

- admission policies;
- project policy bundles;
- secret references;
- provider data policies;
- network policy;
- signed Role packages;
- security audit dashboard.

## Version 0.9 — Extensibility Platform

### Scope

- plugin SDK;
- Role package registry;
- Workflow package registry;
- remote plugin protocol;
- compatibility validation;
- extension health.

## Version 1.0 — Stable AI Team Runtime

### Stability Goals

- stable resource API;
- stable Provider interface;
- stable Capability model;
- stable Workflow versioning;
- migration guarantees;
- production deployment guidance;
- complete security model;
- comprehensive eval suite.

### Product Goals

- local-first installation;
- distributed workers;
- model-agnostic scheduling;
- durable Workflows;
- extensible Knowledge layer;
- auditable human approval;
- reliable software-delivery Workflow.

## Implementation Milestones

### Milestone 1 — Technical Spike

- Ollama structured Planner output;
- local Coding tool loop;
- Codex structured review;
- one temporary Git repository;
- no UI.

### Milestone 2 — Domain and Persistence

- Pydantic resource schemas;
- SQLite;
- repositories;
- Events;
- resource versions;
- Execution CRUD.

### Milestone 3 — Workspace and Capabilities

- Git worktree provider;
- filesystem tools;
- command policy;
- path containment;
- Artifact collection.

### Milestone 4 — Workflow Reconciliation

- Execution controller;
- Work Item controller;
- approval controller;
- retry limits;
- restart recovery.

### Milestone 5 — Minimal UI

- Projects;
- new Execution;
- Plan approval;
- timeline;
- diff;
- review;
- final approval.

### Milestone 6 — Hardening

- integration tests;
- security tests;
- failure recovery;
- logging;
- documentation;
- packaging.

## Evaluation Strategy

Maestro requires repeatable evals.

Initial scenario set:

1. create FastAPI health endpoint;
2. add one field to an existing schema;
3. fix a failing test;
4. update documentation only;
5. reject an unsafe dependency change;
6. recover after Provider outage;
7. handle invalid Planner output;
8. handle Reviewer request changes.

## Quality Metrics

Potential metrics:

- Execution completion rate;
- Planner schema validity;
- Work Item retry rate;
- verification pass rate;
- reviewer rejection rate;
- human rejection rate;
- time to completion;
- number of tool calls;
- policy denial count;
- cost and token usage;
- local versus cloud execution ratio.

## Explicit Non-Goals Before 1.0

- autonomous production deployment;
- unrestricted self-modifying Workflows;
- fully autonomous architecture decisions;
- silent merge;
- hidden long-term model memory;
- arbitrary internet execution;
- multi-tenant SaaS as the primary deployment.

## Roadmap Governance

Roadmap changes should:

- reference Vision and Principles;
- identify affected architecture documents;
- specify new resources or interfaces;
- define migration impact;
- include security implications;
- include measurable exit criteria.

## Design Decisions

- Reliability precedes breadth.
- Software delivery is the first domain.
- Local execution is the default.
- Distributed execution follows the stable local control plane.
- Knowledge and notifications are post-MVP.
- 1.0 requires stable contracts, not every planned feature.

## Open Questions

- Which milestone should introduce LangGraph, if at all?
- Should the first release expose YAML resources publicly?
- When should PostgreSQL become supported?
- Should remote workers precede the Knowledge layer?
- Should Codex remain a reviewer-only integration?
- Which operating systems are officially supported in 0.1?

## Future Evolution

Beyond 1.0:

- research Workflows;
- operations Workflows;
- creative Workflows;
- execution replay and branching;
- policy-based auto-approval;
- multi-Project planning;
- organization-wide knowledge;
- marketplace for Roles, Workflows, and Capabilities;
- AI-team benchmarking suite.

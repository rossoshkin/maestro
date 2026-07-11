# Core Principles

Version: 0.1

## Introduction

This document defines the engineering principles of Maestro.

Every architectural decision should be evaluated against these principles.

When two implementation options exist, prefer the one that aligns more closely with them.

These principles are intentionally stable and should outlive implementation details.

## 1. Human Owns Decisions

Humans own decisions.

AI Roles execute work.

Maestro coordinates work.

Neither Maestro nor any model may silently take ownership of decisions that materially affect a project.

Examples include:

- architectural changes;
- destructive operations;
- dependency upgrades;
- deployments;
- merges;
- changes to security boundaries.

## 2. Maestro Owns the Workflow

Roles do not coordinate one another.

Roles do not decide what happens next.

Roles do not schedule additional work.

Only Maestro owns:

- workflow execution;
- scheduling;
- retries;
- routing;
- state transitions;
- approval gates;
- capability assignment.

## 3. Roles Are Specialists

Each Role has one primary responsibility.

Examples:

- Planner;
- Coding;
- Reviewer;
- Researcher;
- Documentation Writer.

Specialization improves quality, predictability, and accountability.

## 4. Models Are Replaceable

No workflow may depend on a specific model.

Provider-specific APIs must remain behind adapters.

Business logic must not depend on OpenAI, Anthropic, Ollama, Qwen, DeepSeek, Gemini, or any other provider.

## 5. Local First

Everything should work locally whenever practical.

Cloud services are optional enhancements.

The user owns:

- models;
- repositories;
- documents;
- execution history;
- workflow state.

## 6. Persistent State

Models are stateless.

Executions are not.

Every Execution must survive crashes, restarts, provider outages, and model restarts.

Execution state belongs to Maestro.

## 7. Structured Communication

Roles do not communicate through unconstrained free-form conversations.

Every interaction uses a structured contract.

Examples include:

- Plan;
- Work Item;
- Execution Result;
- Review;
- Knowledge Result.

Natural language may exist inside structured fields.

## 8. Explicit Context

Roles receive only the context required for the current Work Item.

Every Role invocation must explicitly define:

- objective;
- constraints;
- repository or Workspace;
- acceptance criteria;
- available Capabilities;
- available knowledge.

Nothing important should depend on hidden chat history.

## 9. Least Privilege

Agents receive the minimum Capabilities required to fulfill their assigned Role.

The Planner should not execute shell commands.

The Reviewer should not modify code.

The Coding Role may modify files and run only approved commands.

## 10. Everything Is Observable

Nothing happens silently.

Maestro stores:

- prompts;
- outputs;
- tool invocations;
- state transitions;
- events;
- file changes;
- artifacts;
- reviews.

Debugging must not depend on guessing.

## 11. Small Iterations

Large goals should be decomposed into small Work Items that are independently executable, testable, reviewable, and reversible.

## 12. Safety Before Speed

Fast execution is never more important than safe execution.

Prefer approval over silent automation.

Prefer verification over assumption.

Prefer rollback over recovery.

## 13. Deterministic Workflows

Models generate content.

Maestro controls execution.

The workflow must not depend on model creativity.

## 14. Event-Driven Coordination

Everything happens because of persisted events.

Roles never directly invoke one another.

Maestro reacts to events and schedules the next Role.

## 15. Separation of Concerns

Domain logic must remain independent from:

- web UI;
- CLI;
- model providers;
- storage;
- Git;
- notifications.

Subsystems communicate through interfaces.

## 16. Knowledge Is External

Knowledge belongs to Knowledge Sources, not to Agents or models.

Possible Knowledge Sources include:

- filesystem;
- NAS;
- Git repositories;
- Odysseus Documents;
- Obsidian;
- Confluence;
- Markdown;
- PDF collections.

## 17. Reproducibility

Every Execution should be reproducible from its repository state, configuration, prompts, workflow version, provider configuration, and recorded artifacts.

Perfect output equality is not required.

Equivalent behavior and traceability are.

## 18. Simplicity

Simple systems survive.

Complex systems require explicit justification.

Prefer one abstraction over unnecessary layers and one workflow over special-case branches.

## 19. Extensibility

New providers, Roles, Capabilities, Knowledge Sources, Workspaces, and notification systems should be addable without modifying Maestro's core domain logic.

## 20. AI Assists Humans

Maestro does not exist to replace software engineers.

It exists to amplify them through better planning, less repetitive work, stronger review, higher quality, and greater transparency.

## Summary

Humans make decisions.

Maestro coordinates work.

Roles define responsibilities.

Agents execute Roles.

Everything is observable, reproducible, secure, and replaceable.

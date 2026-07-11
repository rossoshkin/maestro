# Maestro Development Handbook

## Purpose

This directory contains the implementation specification for Maestro.

Read these documents after `docs/README.md` and before writing any production code.

## Reading Order

1. IMPLEMENTATION_PLAN.md
2. RESOURCE_SPECIFICATION.md
3. ARCHITECTURAL_DECISIONS.md
4. CODING_GUIDELINES.md
5. ARCHITECTURE_CHECKLIST.md
6. TASK_TEMPLATE.md
7. CONTRIBUTING.md
8. CODE_REVIEW_GUIDELINES.md
9. TESTING_GUIDELINES.md
10. ERROR_HANDLING.md
11. LOGGING_GUIDELINES.md
12. API_STYLE_GUIDE.md
13. UI_GUIDELINES.md
14. GIT_WORKFLOW.md
15. RELEASE_PROCESS.md

## Documentation precedence

When documents conflict:

1. ADRs (`docs/adr/`)
2. RESOURCE_SPECIFICATION.md
3. Architecture documents
4. Development documents
5. Source code

Implementation must never override the specification.

## Missing documentation

If implementation requires undocumented behaviour:

- stop implementation
- create `docs/questions/<topic>.md`
- explain the ambiguity
- wait for approval

## Better design proposals

Do not implement architectural improvements immediately.

Create `docs/proposals/<topic>.md` with rationale, migration strategy and tradeoffs.

## Definition of Done

A milestone is complete only when:

- implementation compiles
- tests pass
- documentation updated
- implementation plan updated
- architecture invariants preserved

# Architecture Checklist

Final MVP review: 2026-07-12

Before every merge verify:

- [x] Execution remains aggregate root
- [x] Controllers own state
- [x] Domain has no infrastructure dependencies
- [x] Resources follow RESOURCE_SPECIFICATION.md
- [x] Status written only by controllers or runtime/application services that own
      the specific runtime transition
- [x] Events immutable
- [x] Artifacts immutable
- [x] Tests added
- [x] Documentation updated

MVP evidence:

- `tests/e2e/test_mvp_vertical_slice.py` validates the full local-first
  workflow, restart recovery, repair loop, immutable review evidence, and source
  checkout isolation.
- `tests/test_api.py::test_api_context_sqlite_repositories_work_across_fastapi_threads`
  protects the live FastAPI SQLite context path.
- Failure-scenario coverage is tracked in `docs/development/MVP_DEMO.md`.

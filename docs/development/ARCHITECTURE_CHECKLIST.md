# Architecture Checklist

Before every merge verify:

- [ ] Execution remains aggregate root
- [ ] Controllers own state
- [ ] Domain has no infrastructure dependencies
- [ ] Resources follow RESOURCE_SPECIFICATION.md
- [ ] Status written only by controllers
- [ ] Events immutable
- [ ] Artifacts immutable
- [ ] Tests added
- [ ] Documentation updated

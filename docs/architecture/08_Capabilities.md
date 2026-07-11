# Capabilities

Version: 0.1

Capabilities represent permissions, not implementations.

## Examples

- filesystem.read
- filesystem.write
- shell.execute.test
- git.diff
- knowledge.search
- web.search

## Capability Resolution

Role
+
Project Policy
+
Workflow Policy
+
Workspace Policy
=
Effective Capabilities

## Admission

An Agent cannot execute unless all required Capabilities are granted.

## Invariants

- Deny by default
- Explicit grants
- Tools implement Capabilities
- Models never self-grant permissions

## Future

Capability plugins, signed policies, fine-grained scopes.

# Web API

Version: 0.1

## Purpose

The Web API is the public control plane of Maestro.

Clients never manipulate persistence directly. They interact with resources through a versioned REST API.

## Principles

- Resource-oriented
- Versioned
- Idempotent
- Async-first
- Observable

## Primary Resources

- Projects
- Executions
- Workflows
- Roles
- Agents
- Providers
- Workspaces
- Artifacts
- Reviews

## Example

POST /api/v1/executions

```json
{
  "projectId":"tour-manager",
  "goal":{
    "summary":"Add health endpoint"
  }
}
```

Response:

```json
{
  "executionId":"...",
  "phase":"Planning"
}
```

## Streaming

Future:

- Server Sent Events
- WebSockets
- Event subscriptions

## Errors

Problem Details (RFC7807) style responses.

## Security

Authentication occurs before authorization.

Authorization evaluates Project, Role and Capability policies.

## Future Evolution

GraphQL, gRPC, remote workers API.

# Providers

Version: 0.1

Providers abstract model runtimes.

## Initial Providers

- Ollama
- Codex
- OpenAI
- Anthropic

## Contract

request
→ provider
→ model
→ structured response

Providers expose a unified interface.

## Health

Ready
Degraded
Unavailable

Schedulers avoid unhealthy Providers.

## Invariants

- Provider-independent domain
- Structured outputs
- Retry handled above provider

## Future

Load balancing, multi-provider ensembles, remote clusters.

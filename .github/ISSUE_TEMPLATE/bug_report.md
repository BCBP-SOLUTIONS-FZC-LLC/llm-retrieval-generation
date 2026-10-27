---
name: Bug report
about: Report a defect in the llm-retrieval-generation service (HTTP API, event publishing, caching, or database layer)
title: '[BUG] '
labels: bug
assignees: ''
---

## Description
A clear description of the bug.

## Service version
`github.com/BCBP-SOLUTIONS-FZC-LLC/llm-retrieval-generation` — tag / commit SHA:

## Python version
`python --version`

## Environment
- [ ] Local dev (`make run`)
- [ ] Docker Compose (`make docker-up`)
- [ ] Staging
- [ ] Production

## Affected area
- [ ] HTTP API (endpoint: `METHOD /api/v1/...`)
- [ ] Internal API (endpoint: `METHOD /api/v1/internal/...`)
- [ ] Event publishing (event type: `user.*`)
- [ ] Cache (Valkey)
- [ ] Database / migrations
- [ ] OOO sweep
- [ ] Outbox relay

## Steps to reproduce
1.
2.
3.

## Expected behaviour
What you expected to happen.

## Actual behaviour
What actually happened. Include error messages, HTTP status codes, log output, or stack traces.

```
// paste relevant log output or error here
```

## Minimal reproduction
```go
// paste the smallest snippet or curl command that triggers the bug
```

## Additional context
Any other relevant context (Postgres version, PgBouncer mode, related issues).

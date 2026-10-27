---
name: Feature request
about: Propose a new endpoint, domain field, event type, cache strategy, or service behaviour
title: '[FEAT] '
labels: enhancement
assignees: ''
---

## Problem / motivation
What problem does this solve? Which consumers or use cases are affected?

## Proposed solution
Describe the API or behaviour change you'd like.

```go
// Example: new endpoint, payload struct, or domain type
```

## Affected areas
- [ ] HTTP API (new or changed endpoint)
- [ ] Internal API
- [ ] Domain model (new field or entity)
- [ ] Database schema (new migration required)
- [ ] Event contract (new event type or changed payload — schemas/events update needed)
- [ ] Cache strategy
- [ ] LLM context / RAG integration
- [ ] Helm / deployment config

## Event contract impact
If this adds or changes an event type, describe the downstream consumer impact (AuthZ Enrichment, Workflow Service, Audit Log).

## Alternatives considered
Other approaches you evaluated and why you ruled them out.

## Acceptance criteria
- [ ]
- [ ]
- [ ]

## Additional context
Links to related issues, HLD sections, ADRs, or prior art.

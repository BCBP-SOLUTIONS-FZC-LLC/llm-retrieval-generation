# Architecture diagrams

Standalone Mermaid source files. [`ARCHITECTURE.md`](../../ARCHITECTURE.md) embeds every one of these `.mmd` files verbatim as a fenced code block, each under a `> Source:` link back to the file here — so a diagram only needs to be edited in one place, and the embedded copy in `ARCHITECTURE.md` must be refreshed to match (there is no generator or drift check yet).

| File | Diagram | Related section |
|------|---------|------------|
| [`layer-model.mmd`](mermaid/layer-model.mmd) | Clean Architecture layer graph — the three entry points (API, sweeps, migrate), `presentation/`, `infrastructure/`, `application/` per module, `domain/`, and the vendored `corpusscope` library | ARCHITECTURE.md § Layer model |
| [`package-dependencies.mmd`](mermaid/package-dependencies.mmd) | Python import graph (mirrors `tests/architecture/test_dependency_rules.py`: layering, DEP-1..4) | ARCHITECTURE.md § Package dependency graph, LLD §3.2 |
| [`request-flow.mmd`](mermaid/request-flow.mmd) | R-1 search: identity, scope resolution (Valkey, `llm_corpus`, TAC-4), pipeline, Gateway and Qdrant calls, retrieval-set record | ARCHITECTURE.md § Request flow, LLD §6, §7 |
| [`generation-flow.mmd`](mermaid/generation-flow.mmd) | R-2 answers: admission, two-phase SSE stream, producer task, verification ladder, completion transaction | ARCHITECTURE.md § Generation flow, LLD §7, §8 |
| [`workflow-flow.mmd`](mermaid/workflow-flow.mmd) | W-1 start and W-3 approve: admission-then-engine ordering, scope re-resolution, guarded gate update, `scope_revoked` | ARCHITECTURE.md § Workflow flow, LLD §9 |
| [`event-outbox-flow.mmd`](mermaid/event-outbox-flow.mmd) | Enqueue validation → outbox on each database → relays → `RoutingPublisher` → two SNS topics; the six consumer queues and the poison path | ARCHITECTURE.md § Event and outbox flow, LLD §12 |
| [`cache-strategy.mmd`](mermaid/cache-strategy.mmd) | Valkey key namespaces, TTLs, read and invalidation paths, the known gaps | ARCHITECTURE.md § Cache strategy, LLD §11 |
| [`data-model.mmd`](mermaid/data-model.mmd) | ER diagram of the 5 `llm_corpus` and 19 `llm_generation` tables (no FK across the two) | ARCHITECTURE.md § Data model, LLD §4 |
| [`rls-guc-flow.mmd`](mermaid/rls-guc-flow.mmd) | `app.tenant_id` GUC injection per request or consumer (RLS-6) behind PgBouncer | ARCHITECTURE.md § RLS and GUC injection, LLD §4.8 |
| [`observability-stack.mmd`](mermaid/observability-stack.mmd) | Logs, spans and Tier-3 metrics, and why metrics are off by default | ARCHITECTURE.md § Observability stack, LLD §16 |

The diagrams have been written by hand and **not rendered by a Mermaid tool**. To render locally, open any `.mmd` file in a Mermaid-aware IDE (VS Code + Mermaid Preview, IntelliJ + Mermaid plugin) or paste into [mermaid.live](https://mermaid.live) — and fix any syntax error you find here and in `ARCHITECTURE.md`.

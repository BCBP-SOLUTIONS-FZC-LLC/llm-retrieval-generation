# Observability — llm-retrieval-generation

How this service implements the platform observability standard through [`platform-fastapicommon`](https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon). Logs, spans and Tier-3 metrics all go through the library; this service builds only its own collectors.

> **Status: metrics are disabled by default.** The platform metrics registry has no `llm` domain yet (LLD OQ-103), and `create_platform_app` raises on any configured domain outside it. Until it is registered, `OBSERVABILITY_DOMAIN` stays unset and `ENABLE_METRICS` stays `false`; the Prometheus observers are never built, and logs and traces are the only signal. No alert rules, dashboards or SLO rules exist yet — the alerts in [`runbooks.md`](runbooks.md) are **proposed** and keyed on logs and database state where no metric can be used.

| Piece | Location |
|---|---|
| Metric inventory (every metric this service declares, by group) | [`metric-registry.md`](metric-registry.md) |
| Collector construction and the `llm_retrieval_generation_` prefix | `src/rag/infrastructure/observability/{metrics,generation_metrics,workflow_metrics}.py` |
| Structured logging and tracing wrappers | the same modules (`LoggingObserver`, `TracingGenerationObserver`, `TracingWorkflowObserver`) and `observability/logs.py` |
| Name and label checks | `tests/unit/test_retrieval_infra.py`, `tests/unit/test_generation_infra.py`, `tests/unit/test_workflows_wiring.py` (names validated against the platform registry without a scrape) |
| Wiring tests for the adapters | `tests/unit/test_observability_adapters.py` |
| Settings | `ENABLE_METRICS`, `ENABLE_TRACING`, `OBSERVABILITY_DOMAIN`, `APP_NAME`, `APP_ENV` (`src/rag/infrastructure/config.py`) |
| Alert runbooks | [`runbooks.md`](runbooks.md) and the `docs/runbook-*.md` files |

## Platform libraries

| Library | Version | Emits here | Wiring |
|---|---|---|---|
| `platform-fastapicommon` | 0.4.1 | `platform_http_*`, build info, tracing, structured logs; owns the registry and the `<domain>_<service>_` prefix rule | `create_platform_app(settings=…)` in `presentation/main.py` |
| `eventcommon` | 0.4.0 | `platform_messages_*`, `platform_outbox_*`, `platform_dlq_messages_total`, inbox counters | `metrics.init_metrics` in `start_event_workers`, only when `ENABLE_METRICS` |
| `pgcommon` | 0.4.1 | `platform_db_*` per pool | `pgmetrics_init_with_identity` in the lifespan, only when `ENABLE_METRICS` |

## Identity

| Label | Value | Source |
|---|---|---|
| `service` | `retrieval-generation` | `Settings.app_name` |
| `environment` | `dev`, `test`, `staging`, `production` | `APP_ENV` |
| `domain` | `llm` — **not yet registered** (OQ-103) | `OBSERVABILITY_DOMAIN` |

## Conformance with the Enterprise Platform Observability Standard

| Tier | Here | How it is held |
|---|---|---|
| 1, `platform_*` | Emitted by the libraries only (`platform_http_*`, `platform_messages_*`, `platform_outbox_*`, `platform_dlq_messages_total`, `platform_db_*`). This service declares none. | `tests/architecture/test_metrics_standard.py` fails on a `platform_*` literal or any `new_shared_*` call |
| 2, `<domain>_*` | None. `llm` has one service in this repo; a metric shared with Ingestion or the Worker Pool would need the domain registry first. | Same test |
| 3, `llm_retrieval_generation_*` | Every collector in [`metric-registry.md`](metric-registry.md). | Prefix, `_total` / `_seconds` suffixes, label vocabulary and `check_custom` registry rules, for all three modules |

- `domain`, `service` and `environment` are injected by the library's registry, never declared (the test rejects them, and `tenant_id`, `user_id`, `email`, `request_id`, `event_id`, `session_id`, as labels).
- The inventory document must list exactly the declared collectors, in both directions.
- **Proposed platform metrics not used** (`platform_dependency_request_seconds`, `platform_queue_depth`, `platform_dlq_depth`, `platform_duplicate_messages_total`, `platform_event_propagation_seconds`): awaiting registry ratification, so no service-side stand-ins exist. When ratified, create them with `new_shared_*` and add the use here.
- **Backward compatibility:** no legacy metric exists (the service has never been deployed or scraped), so there is no old-and-new parallel emission, sunset period or removal to run.
- **Dashboards, alert rules, recording rules, SLOs and HPA references do not exist yet.** They are blocked on the `llm` domain registration (OQ-103); the proposed alerts are in [`runbooks.md`](runbooks.md). Nothing to migrate until then.

## Rules this service follows

- **Names:** Tier-3 metrics are `llm_retrieval_generation_<metric>` (LLD §16, OBS-3). `fastapicommon` rejects any service metric that does not start with `<domain>_<service>_`.
- **Shapes forced by the standard:** a ratio is not a duration, so a "ratio" histogram becomes a **counter per band** (`candidate_depth_band_total`, `tier0_disagreement_band_total`, `groundedness_band_total`) and a rate becomes a **gauge** (`retrieval_cache_hit_ratio`, `verdict_frame_delivered_ratio`).
- **Bounded labels:** stage names, marker reasons, verdicts, claim outcomes, ladder tiers, delivery states, bands, pack-declared section types (anything else is `other`), pack roles, Gateway model profiles. **Never** a tenant, user, query, chunk, or id (OBS-2).
- **Logs** are written only with `fastapicommon.logging.get_logger` (structured: an event name plus fields; no module uses stdlib `logging`, enforced by `tests/architecture/test_shared_libs_only.py`) and carry the tenant and request ids from the request context the library binds. Never logged: the query, chunk text, refinement content, ids as message text.
- **Spans:** one per pipeline stage under the request's trace; an escaping error is recorded by exception type only (`record_span_error`), never its message.

## Enabling metrics (when `llm` is registered)

1. Set `OBSERVABILITY_DOMAIN=llm` and `ENABLE_METRICS=true`.
2. Confirm startup does not raise from `create_platform_app`.
3. Scrape the service and check each name in [`metric-registry.md`](metric-registry.md) appears.
4. Add alert rules, recording rules and dashboards, and turn the proposed alerts in [`runbooks.md`](runbooks.md) into real ones.

# API, Caching and Events

The generated contracts are authoritative and committed: [`docs/swagger/swagger.json`](../docs/swagger/swagger.json), [`api/asyncapi.yaml`](../api/asyncapi.yaml), [`schemas/events/`](../schemas/events/). This file is the map. After any route, model or event change run `make swag`.

## 5. API

### 5.1 Prefixes and identity

| Prefix | Callers | Identity |
|---|---|---|
| `/api/v1/*` | Tender and other tenant-facing callers via the gateway | `x-tenant-id`, `x-user-id`, `x-tenant-roles` (+ `x-departments`, `x-plan`, `x-feature-flags`, `x-residency-mode`) |
| `/api/v1/internal/collections/*` | Ingestion, in-mesh only | `x-tenant-id` only (`require_tenant`) |
| `/api/v1/operator/*` | `platform_operator` only | role read from `x-tenant-roles` |
| `/api/internal/models` (C-M1) | User Profile | the one route with no auth dependency (named CI-1 exemption) |

### 5.2 Public routes (27)

| ID | Route | Handler module | Role |
|---|---|---|---|
| R-1 | `POST /search` | `api/public/retrieval.py` | member |
| R-2 | `POST /answers` (SSE) | `api/public/generation.py` | member |
| R-3 | `POST /sections/drafts` (SSE) | `api/public/generation.py` | member |
| R-4 | `POST /extractions` | `api/public/generation.py` | member |
| R-5 | `GET /retrieval-sets/{id}` | `api/public/artefacts_retrieval_sets.py` | member |
| R-6 | `POST /classification/propose` | `api/public/retrieval.py` | member |
| R-7 | `POST /required-input-checks` | `api/public/retrieval.py` | member |
| A-1..A-4, A-8, A-9 | `GET /artefacts/{id}[/versions[/{no}]\|/sections\|/render\|/sections/{ref}/versions/{no}/content]` | `api/public/artefacts.py` | member |
| A-5, A-6 | `POST /artefacts/{id}/regenerate`, `…/sections/{ref}/regenerate` (SSE) | `api/public/regeneration.py` | member |
| A-7 | `POST …/sections/{ref}/fact-conflict-resolution` | `api/public/regeneration.py` | member |
| W-1..W-5, W-7 | `/generations[/{id}[/outline[/approve]\|/cancel\|/resume]]` | `api/public/workflows.py` | member |
| W-6 | `POST /extractions/async` | `api/public/workflows.py` | member |
| P-1, P-2 | `GET /collections[/{id}]` | `api/public/corpus.py` | member |
| P-3 | `GET /collections/{id}/grants` | `api/public/corpus.py` | `tenant_admin` or `tender_admin` |
| P-6 | `PUT /collections/{id}/retention` | `api/public/corpus.py` | `tenant_admin` |

### 5.3 Internal, operator and catalogue

C-I1 `POST /resolve`, C-I2 `GET /{id}`, C-I4 `POST /{id}/index-generations/{n}/activate`, C-I5 `GET /{id}/retention`, C-I6 `POST /{id}/purge-epoch`, C-I8 `POST /{id}/migration-result` (all `api/internal/corpus.py`); W-8 `POST /api/v1/operator/generations/{id}/rematerialise` (`api/operator/generations.py`); C-M1 `GET /api/internal/models` (`api/internal/models.py`). **Reserved:** C-I3, C-I9. **Retired:** C-T1, C-T2. A-8 is mounted and answers `503 RENDERER_UNAVAILABLE`.

### 5.4 Conventions

- **Status commits at the first byte** of an SSE body (LD-2): everything that can be an HTTP problem runs before the stream opens (API-6). After it, failure is a terminal `error` frame on the `200`.
- `206` where retrieval or extraction degraded; `202` + handle for async starts; `200` on an idempotent replay; `201` vs `200` on C-I1 and P-6 (create vs existing).
- Pagination: `limit` (default 100, clamped to 500) and `offset`.
- Errors: RFC 9457 problem bodies, 32 codes (`domain/shared/errors.py`, `presentation/errors.py`).
- `Idempotency-Key` on W-1 and W-6 (TTL 1 h); a failed or cancelled run releases its key.

## 6. Caching (Valkey; an outage is a miss)

| Cache | Key | TTL | Invalidation |
|---|---|---|---|
| Scope | tenant, principal, roles → `ScopeToken` | 60 s (`SCOPE_CACHE_TTL_SECONDS`); token 600 s | Expiry only; the purge epoch rides the token, so a purge can show up to 60 s late. Scope-cache `DEL` on collection writes is **not built** |
| Pack | `{tenant}:pack:{domain}:{language}:{concern}:{version}` | per pack | `DomainProfilePublished` → `SCAN` + `DEL` (`ValkeyProfileCache`) |
| Retrieval set | tenant-prefixed | 15 min | None; a document revision does not invalidate it (LD-38) |
| Placement | `{tenant}:coll:{collection_id}` | — | `DEL` on a purge-epoch bump (`ValkeyPlacementCache`) |
| IAM user context | per user | 300 s | Expiry; 300 ms budget, breaker |
| Idempotency | per key | 1 h | `idempotency-keys` sweep clears them in Postgres |

The pack-cache key shape is an **assumption** (§11.1 does not fix the identity).

## 7. Events

### 7.1 Envelope

`id` (uuidv7), `type`, `source` (`llm-rag-service`), `tenant_id`, `trace_id`, `correlation_id`, `subject` (`tenders/<tender_id>` or absent), `time`, `data`. Message attributes: `EventID`, `TenantID`, `EventType`, `Source`, `Subject`, `traceparent`. **`tenant_id` is never in `data`.**

### 7.2 Published (17)

| Topic | Types |
|---|---|
| `llm-generation-events` | `GenerationStarted`, `GenerationGapReported`, `OutlineReady`, `SectionCompleted`, `GenerationProgressed`, `GenerationCompleted`, `GenerationFailed`, `GenerationCancelled`, `ArtefactRegenerated`, `ExtractionCompleted` |
| `llm-index-events` | `IndexGenerationActivated`, `IndexGenerationFailed`, `CorpusConflictDetected`, `CollectionProvisioned`, `CollectionPromoted`, `CollectionStatusChanged`, `ProfileCacheRefreshed` |

Emitted today: the corpus events, `ProfileCacheRefreshed`, `GenerationFailed` (sweeps and `scope_revoked`), `GenerationStarted` (every admission), `GenerationProgressed` (W-3/W-4/W-7), `GenerationCancelled` (W-5), `SectionCompleted` with `origin = regeneration` (A-6). **Still the Worker Pool's:** `OutlineReady`, `GenerationGapReported`, the activities' `GenerationProgressed` / `SectionCompleted` / `GenerationCompleted` / `ArtefactRegenerated`. `CorpusConflictDetected` is deferred past v1 (LD-52). No event is published for a failed or cancelled run beyond the cases above (audit entries only).

### 7.3 Consumed (11 types, 5 queues)

| Queue | Consumer ledger | Codec | Events |
|---|---|---|---|
| `llm-document-rag-q` | `purge_projection` | plain | `DocumentPurged` |
| `domainprofile-llm-invalidate-q` | `pack_invalidation` | plain | `DomainProfilePublished` |
| `membership-rag-q` | `membership` | Glue | `TenantMembershipsPurged`, `TenantStateChanged` |
| `tender-rag-q` | `tender_lifecycle` | plain | `TenderRestricted`, `TenderPurged`, the four terminal outcomes (`TenderDeclined/Won/Lost/Withdrawn`, names assumed) |
| `usage-rag-q` | `quota_resume` | plain | `TenantQuotaRestored` |

Poison goes to the queue's DLQ at the fourth receive (`SQS_MAX_RECEIVE_COUNT=3`, eventcommon forwards when the count *exceeds* it) and is acknowledged. The authoritative lists are `runtime.QUEUES` and `contracts.py`.

### 7.4 Rules

- Enqueue inside the state change's transaction; an unregistered type or off-schema payload fails closed (EVT-9).
- One relay per database, one routing publisher; topic by event type name.
- Schemas allow additional properties (LD-65): additive fields are not breaking.
- Not built: the Glue **encoder** for published events (LD-82).

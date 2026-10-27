# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x     | ✅ Active |

Older major versions are not patched. Deploy the latest 1.x image.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Email: vijay@bcbpsolutions.com
Subject: `[llm-retrieval-generation] Security vulnerability`

Include in your report:
- Description of the vulnerability and the affected component (HTTP handler, RLS policy, event payload, cache, etc.)
- Steps to reproduce
- Potential impact (PII exposure, RLS bypass, tenant data leakage, privilege escalation, DoS, etc.)
- Suggested fix or patch (if any)

### Response timeline

| Step | Target |
|------|--------|
| Initial acknowledgement | 48 hours |
| Severity assessment | 5 business days |
| Patch release (critical/high) | 14 days |
| Public disclosure | After patch ships |

We follow responsible disclosure. Reporters will be credited in release notes unless anonymity is requested.

## Scope

Areas of particular sensitivity in this service:

- **Row-Level Security (RLS)** — tenant isolation enforced at the Postgres layer; a bypass would expose cross-tenant corpus and generation data.
- **GUC injection** — `app.tenant_id` and `app.user_id` are set per-request via `GUCBridgeMiddleware`; incorrect values would allow privilege escalation.
- **Signature S3 keys** — `signature_key` is a bare S3 object key used by the Approver workflow; exposure could allow forged approvals.
- **LLM context** — `GET /internal/users/:id/llm-context` is consumed by the RAG & Generation Service; injection here could influence AI-generated output.
- **Outbox payloads** — event payloads are validated against JSON Schemas before insert; a bypass could propagate malformed data to downstream consumers.

---
name: peer-contract-check
description: Background rule for code that calls or consumes another service (IAM, Tender, Ingestion, Worker Pool, Model Gateway). Peer contracts are assumptions until the peer's own doc confirms them.
user-invocable: false
---

Before changing a peer call, body shape, queue or event:

1. Read the peer's entry in `docs/implementation-plan.md` (assumed peer contracts) and the peer's own doc if one exists.
2. Cite the doc and section for any field, path or status you rely on. If there is none, say it is an assumption.
3. Record new or changed assumptions in `docs/implementation-plan.md`; never harden a guess into a fact in code comments or docs.
4. Keep the service agnostic of the peer's internals; read only the fields we use.

---
name: pr-check
description: Run the full pre-PR gate set for this service and report pass/fail per gate.
disable-model-invocation: true
---

Run from the repo root and report each gate's result. Stop and show the output of the first real failure.

Needs: network for `vuln-check`, Docker for steps 2 and 3. If one is unavailable, report that gate as `skipped (no Docker)` or `skipped (offline)`, run the rest, and say the verdict is partial.

1. `make lint typecheck rls-check swag-check lock-check vuln-check helm-check`
2. `make cover-check` (needs Docker; unit + integration, 95% coverage gate)
3. `make test-e2e` (needs Docker)

Do not edit generated files to fix `swag-check`; run `make swag`. Finish with a short verdict: ready for a PR, or what blocks it.

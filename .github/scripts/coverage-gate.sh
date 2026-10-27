#!/usr/bin/env bash
# Enforces minimum total test coverage (default 95%).
set -euo pipefail

THRESHOLD="${COVERAGE_THRESHOLD:-95}"
PY="${PY:-.venv/bin/python}"

test -f coverage.xml || {
  echo "::error file=coverage.xml,title=Coverage gate::coverage.xml missing — run 'make test-ci' before the coverage gate"
  exit 1
}

pct=$("$PY" - <<'PYEOF'
import xml.etree.ElementTree as ET
print(f"{float(ET.parse('coverage.xml').getroot().get('line-rate')) * 100:.2f}")
PYEOF
)
echo "Total coverage: ${pct}%"

# Emit before the gate check so the value is available even when coverage fails.
[ -n "${GITHUB_OUTPUT:-}" ] && echo "pct=${pct}" >> "$GITHUB_OUTPUT"

gate=$(awk -v p="$pct" -v t="$THRESHOLD" 'BEGIN { print (p+0 < t) ? "FAIL" : "OK" }')
if [ "${gate}" = "FAIL" ]; then
  echo "::error file=coverage.xml,title=Coverage gate::Coverage is ${pct}% — below the ${THRESHOLD}% threshold. Run 'make cover-func' locally to identify gaps."
  exit 1
fi

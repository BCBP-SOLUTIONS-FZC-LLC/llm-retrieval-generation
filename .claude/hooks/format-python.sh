#!/usr/bin/env bash
# PostToolUse: keep edited Python files ruff-clean (CI runs `ruff format --check`).
# Guardrail only: silently skips when jq or the project's ruff is missing (run `make setup`).
command -v jq >/dev/null || { echo "format hook skipped: jq not installed" >&2; exit 0; }
ruff="$CLAUDE_PROJECT_DIR/.venv/bin/ruff"
[ -x "$ruff" ] || exit 0
f=$(jq -r '.tool_input.file_path // empty')
case "$f" in "$CLAUDE_PROJECT_DIR"/*.py) "$ruff" format --quiet "$f" ;; esac
exit 0

#!/usr/bin/env bash
# PreToolUse: generated files are never hand-edited; change the code and run `make swag`.
# Guardrail for Edit/Write only. A shell write can still bypass it; CI's `swag-check` is the backstop.
command -v jq >/dev/null || { echo "generated-file guard skipped: jq not installed" >&2; exit 0; }
f=$(jq -r '.tool_input.file_path // empty')
case "$f" in
  */docs/swagger/*|*/api/asyncapi.*|*/schemas/events/*.json)
    echo "Generated file: change the code and run 'make swag' instead of editing $f." >&2
    exit 2 ;;
esac
exit 0

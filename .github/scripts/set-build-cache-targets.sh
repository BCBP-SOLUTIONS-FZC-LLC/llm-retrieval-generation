#!/usr/bin/env bash
# Writes docker/build-push-action cache-from/cache-to multiline outputs.
# Invoked by ci.yml build-image job (keeps heredoc markers out of inline YAML).
set -euo pipefail

: "${REGISTRY_REF:?REGISTRY_REF is required}"
: "${CAN_PUSH:?CAN_PUSH is required}"

{
  echo 'from<<EOF'
  echo 'type=gha'
  echo "type=registry,ref=${REGISTRY_REF}"
  echo 'EOF'
} >> "$GITHUB_OUTPUT"

if [ "${CAN_PUSH}" = 'true' ]; then
  {
    echo 'to<<EOF'
    echo 'type=gha,mode=max'
    echo "type=registry,ref=${REGISTRY_REF},mode=max"
    echo 'EOF'
  } >> "$GITHUB_OUTPUT"
else
  {
    echo 'to<<EOF'
    echo 'type=gha,mode=max'
    echo 'EOF'
  } >> "$GITHUB_OUTPUT"
fi

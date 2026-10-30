#!/usr/bin/env bash
set -euo pipefail
SDE=$(git log -1 --format=%ct)
echo "SOURCE_DATE_EPOCH=${SDE}" >> "$GITHUB_ENV"
echo "SOURCE_DATE_EPOCH=${SDE}" >> "$GITHUB_OUTPUT"

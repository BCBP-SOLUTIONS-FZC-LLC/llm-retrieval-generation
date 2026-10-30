#!/usr/bin/env bash
# Appends published image digest and tags to the GitHub Actions step summary.
set -euo pipefail

: "${IMAGE_DIGEST:?IMAGE_DIGEST is required}"
: "${IMAGE_TAGS:?IMAGE_TAGS is required}"

{
  echo "### Published image"
  echo "**Digest:** \`${IMAGE_DIGEST}\`"
  echo "**Tags:**"
  echo "${IMAGE_TAGS}" | tr ',' '\n' | sed 's/^/- `/' | sed 's/$/ `/'
} >> "$GITHUB_STEP_SUMMARY"

#!/usr/bin/env bash
# Ensures checked-out HEAD matches RELEASE_TAG (exact tag, not branch tip).
set -euo pipefail

: "${RELEASE_TAG:?RELEASE_TAG is required}"

if ! test "$RELEASE_TAG" = "$(git describe --tags --exact-match)"; then
  echo "::error title=Tag mismatch::RELEASE_TAG=${RELEASE_TAG} does not match git describe --tags --exact-match at HEAD"
  exit 1
fi
echo "  ✔  tag verified: ${RELEASE_TAG}"

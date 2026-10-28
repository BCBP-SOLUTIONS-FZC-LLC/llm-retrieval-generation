#!/usr/bin/env bash
# Fails the release if CHANGELOG.md has no section for RELEASE_TAG.
set -euo pipefail

: "${RELEASE_TAG:?RELEASE_TAG is required}"

VERSION="${RELEASE_TAG#v}"
if ! grep -q "## \[${VERSION}\]" CHANGELOG.md 2>/dev/null; then
  echo "::error file=CHANGELOG.md::Missing entry for ${VERSION}"
  exit 1
fi
echo "  ✔  changelog entry found for ${VERSION}"

#!/usr/bin/env bash
# Extracts RELEASE_TAG section from CHANGELOG.md and appends image metadata.
set -euo pipefail

: "${RELEASE_TAG:?RELEASE_TAG is required}"
: "${IMAGE_NAME:?IMAGE_NAME is required}"
: "${IMAGE_VERSION:?IMAGE_VERSION is required}"
: "${IMAGE_DIGEST:?IMAGE_DIGEST is required}"

VERSION="${RELEASE_TAG#v}"
awk "/^## \\[${VERSION}\\]/{found=1; next} /^## \\[/{if(found) exit} found{print}" CHANGELOG.md \
  | sed -e 's/^[[:space:]]*$//' \
  | tee release-notes.md | true

if [ ! -s release-notes.md ]; then
  echo "::error file=CHANGELOG.md::Missing entry for ${VERSION}"
  exit 1
fi

{
  cat release-notes.md
  echo ""
  echo "---"
  echo "**Docker image:** \`${IMAGE_NAME}:${IMAGE_VERSION}\`"
  echo "**Digest:** \`${IMAGE_DIGEST}\`"
} | tee release-notes.md.tmp | true
mv release-notes.md.tmp release-notes.md

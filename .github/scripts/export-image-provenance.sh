#!/usr/bin/env bash
# Downloads SLSA provenance attestation from GHCR and writes provenance.slsa.json.
set -euo pipefail
set -o pipefail

: "${IMAGE_NAME:?IMAGE_NAME is required}"
: "${IMAGE_DIGEST:?IMAGE_DIGEST is required}"

IMAGE_REF="${IMAGE_NAME}@${IMAGE_DIGEST}"

if ! cosign download attestation \
  --predicate-type=https://slsa.dev/provenance/v1 \
  "${IMAGE_REF}" \
  | jq -er 'if type == "array" then .[0].payload else .payload end' \
  | base64 -d \
  | jq . \
  | tee provenance.slsa.json >/dev/null; then
  echo "::error title=Provenance export::SLSA provenance attestation not found on ${IMAGE_REF}"
  exit 1
fi

echo "  ✔  exported SLSA provenance to provenance.slsa.json"

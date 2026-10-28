#!/usr/bin/env bash
# helm lint --strict and helm template for the base values and each environment overlay, then
# kubeconform over every rendered manifest. Uses `helm` / `kubeconform` from PATH when present
# (CI runners have helm), else the pinned Docker images, so it needs no local install.
set -euo pipefail

cd "$(dirname "$0")/../.."
CHART=deploy/helm
HELM_IMAGE=alpine/helm:3.16.3
KUBECONFORM_IMAGE=ghcr.io/yannh/kubeconform:v0.6.7
K8S_VERSION=1.31.0

helm_run() {
  if command -v helm >/dev/null 2>&1; then
    helm "$@"
  else
    docker run --rm -v "$PWD/$CHART":/chart "$HELM_IMAGE" "$@"
  fi
}
chart_path() { if command -v helm >/dev/null 2>&1; then echo "$CHART"; else echo /chart; fi; }
conform() {
  if command -v kubeconform >/dev/null 2>&1; then
    kubeconform "$@"
  else
    docker run --rm -i "$KUBECONFORM_IMAGE" "$@"
  fi
}

# the required secret keys, as throwaway values: the chart refuses to render without them
SET='secretValues.CORPUS_DATABASE_URL=x,secretValues.GENERATION_DATABASE_URL=x,secretValues.CORPUS_MIGRATION_DATABASE_URL=x,secretValues.GENERATION_MIGRATION_DATABASE_URL=x,secretValues.VALKEY_URL=x'
# the optional templates, so they are rendered and validated too
EXTRA='ingress.enabled=true,ingress.gateway.name=gw,serviceMonitor.enabled=true'
P=$(chart_path)

combos=("" "values-dev.yaml" "values-staging.yaml" "values-prod.yaml")
for overlay in "${combos[@]}"; do
  args=()
  [ -n "$overlay" ] && args=(-f "$P/$overlay")
  label=${overlay:-base}
  echo "== helm lint ($label)"
  helm_run lint "$P" --strict "${args[@]}" --set "$SET"
  echo "== helm template + kubeconform ($label)"
  helm_run template rel "$P" "${args[@]}" --set "$SET,$EXTRA" |
    conform -strict -ignore-missing-schemas -kubernetes-version "$K8S_VERSION" -summary
done

echo "== existingSecret mode renders no Secret"
count=$(helm_run template rel "$P" --set existingSecret=my-secrets | grep -c '^kind: Secret' || true)
[ "$count" = 0 ] || { echo "expected no Secret with existingSecret, found $count"; exit 1; }

echo "== a missing required secret key fails the render"
if helm_run template rel "$P" >/dev/null 2>&1; then
  echo "render succeeded without the required secret keys"
  exit 1
fi
echo "== dev-only services never reach a deployable"
if grep -rniE 'ollama|open-webui|model-stub|vllm' deploy/helm Dockerfile .dockerignore 2>/dev/null | grep -viE '^\S+:[0-9]+:\s*#|^\.dockerignore'; then
  echo "the chart or the Dockerfile references a dev-only service (Ollama, vLLM, Open WebUI, the model stub)"
  exit 1
fi
echo "helm-check passed"

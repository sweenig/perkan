#!/usr/bin/env bash
set -euo pipefail

IMAGE="sweenig/perkan"
PLATFORMS="linux/amd64,linux/arm64"

# Accept version as first argument, otherwise prompt
VERSION="${1:-}"

if [[ -z "$VERSION" ]]; then
  read -rp "Enter version (e.g. 0.9.2): " VERSION
fi

if [[ -z "$VERSION" ]]; then
  echo "❌ Version cannot be empty"
  exit 1
fi

echo "� Building and pushing:"
echo "  - ${IMAGE}:latest"
echo "  - ${IMAGE}:${VERSION}"
echo "  - Platforms: ${PLATFORMS}"
echo

docker buildx build \
  --platform "${PLATFORMS}" \
  -t "${IMAGE}:latest" \
  -t "${IMAGE}:${VERSION}" \
  --push \
  .

echo
echo "✅ Done!"

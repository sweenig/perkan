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

# Multi-arch builds need QEMU emulation for the non-native platform. It is lost
# when Docker/WSL restarts, so check for it and (re)register it when missing.
check_emulation() {
  docker run --rm --platform linux/arm64 busybox true >/dev/null 2>&1
}

if ! check_emulation; then
  echo "⚙️  arm64 emulation not available; registering QEMU binfmt handlers (needs a privileged container)..."
  docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null
  if ! check_emulation; then
    echo "❌ arm64 emulation still unavailable after install"
    exit 1
  fi
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

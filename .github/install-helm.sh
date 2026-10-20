#!/usr/bin/env bash
# Install the pinned Linux AMD64 CLI used by GitHub Actions.
set -euo pipefail
VERSION=v3.19.5
EXPECTED=a0a5e8c592ed3f376ac110715eff214730c7422f9a44d96cf98117d2b8b0e6c0
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl --fail --silent --show-error --location --retry 3 --proto '=https' --tlsv1.2 \
  "https://get.helm.sh/helm-${VERSION}-linux-amd64.tar.gz" -o "$work/helm.tar.gz"
printf '%s  %s\n' "$EXPECTED" "$work/helm.tar.gz" | sha256sum --check --strict
tar -xzf "$work/helm.tar.gz" -C "$work" linux-amd64/helm
mkdir -p "$HOME/.local/bin"
install -m 0755 "$work/linux-amd64/helm" "$HOME/.local/bin/helm"

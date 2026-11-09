#!/usr/bin/env bash
# Install the pinned Linux AMD64 CLI used for offline deployment rendering.
set -euo pipefail
VERSION=v1.34.3
EXPECTED=ab60ca5f0fd60c1eb81b52909e67060e3ba0bd27e55a8ac147cbc2172ff14212
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl --fail --silent --show-error --location --retry 3 --proto '=https' --tlsv1.2 \
  "https://dl.k8s.io/release/${VERSION}/bin/linux/amd64/kubectl" -o "$work/kubectl"
printf '%s  %s\n' "$EXPECTED" "$work/kubectl" | sha256sum --check --strict
mkdir -p "$HOME/.local/bin"
install -m 0755 "$work/kubectl" "$HOME/.local/bin/kubectl"

#!/usr/bin/env bash
# Builds the SimpleVM provider from source and writes a CLI config that points
# Terraform/OpenTofu at the local binary via dev_overrides. Needed until
# deNBI/simplevm is published to the registries.
#
# Environment:
#   PROVIDER_REPO  git URL of the provider (default: deNBI GitHub, via SSH)
#   PROVIDER_REF   branch, tag or commit to build (default: main)
set -euo pipefail

repo="${PROVIDER_REPO:-git@github.com:deNBI/terraform-provider-simplevm.git}"
ref="${PROVIDER_REF:-main}"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dir="$root/.provider"
src="$dir/src"
bin="$dir/bin"

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v go >/dev/null || { echo "Go is required to build the provider (https://go.dev/dl/)" >&2; exit 1; }

if [ -d "$src/.git" ]; then
  git -C "$src" fetch --quiet origin
else
  git clone --quiet "$repo" "$src"
fi
git -C "$src" -c advice.detachedHead=false checkout --quiet "$ref"
# Fast-forward when $ref is a branch; a no-op for tags and commits.
git -C "$src" merge --quiet --ff-only "origin/$ref" 2>/dev/null || true

mkdir -p "$bin"
(cd "$src" && go build -o "$bin/terraform-provider-simplevm" .)

cat > "$dir/dev.tfrc" <<TFRC
provider_installation {
  dev_overrides {
    "deNBI/simplevm"                       = "$bin"
    "registry.opentofu.org/deNBI/simplevm"   = "$bin"
  }
  direct {}
}
TFRC

echo "Built provider $(git -C "$src" rev-parse --short HEAD) ($ref) into $bin"
echo "CLI config: $dir/dev.tfrc  (the Makefile sets TF_CLI_CONFIG_FILE to it)"

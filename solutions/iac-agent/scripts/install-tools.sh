#!/usr/bin/env bash
# Installs the exact tool versions in tools.lock.json into .tools/ (gitignored),
# verifying every download against the publisher's SHA256 checksums. Nothing is
# installed system-wide, so other projects keep their own Terraform.
#
# Usage: scripts/install-tools.sh     then: export PATH="$PWD/.tools/bin:$PATH"
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
lock="$here/tools.lock.json"
bin="$here/.tools/bin"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$bin"

v() { jq -r --arg k "$1" '.[$k]' "$lock"; }
os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in
  arm64 | aarch64) arch=arm64 ;;
  x86_64) arch=amd64 ;;
  *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

# fetch <url> <checksums-url> <file-name-in-checksums>
fetch() {
  curl -fsSL "$1" -o "$tmp/$3"
  curl -fsSL "$2" -o "$tmp/sums"
  (cd "$tmp" && grep -E "  $3\$" sums | shasum -a 256 -c -) || { echo "checksum mismatch: $3" >&2; exit 1; }
}

tf="$(v terraform)"
f="terraform_${tf}_${os}_${arch}.zip"
fetch "https://releases.hashicorp.com/terraform/$tf/$f" "https://releases.hashicorp.com/terraform/$tf/terraform_${tf}_SHA256SUMS" "$f"
unzip -oq "$tmp/$f" terraform -d "$bin"

tl="$(v tflint)"
f="tflint_${os}_${arch}.zip"
fetch "https://github.com/terraform-linters/tflint/releases/download/v$tl/$f" \
  "https://github.com/terraform-linters/tflint/releases/download/v$tl/checksums.txt" "$f"
unzip -oq "$tmp/$f" tflint -d "$bin"

gl="$(v gitleaks)"
gl_arch="$arch"; [[ "$arch" == amd64 ]] && gl_arch=x64
f="gitleaks_${gl}_${os}_${gl_arch}.tar.gz"
fetch "https://github.com/gitleaks/gitleaks/releases/download/v$gl/$f" \
  "https://github.com/gitleaks/gitleaks/releases/download/v$gl/gitleaks_${gl}_checksums.txt" "$f"
tar -xzf "$tmp/$f" -C "$bin" gitleaks

# tflint's AWS ruleset: tflint --init downloads it and verifies the signed checksums itself.
mkdir -p "$here/.tools/tflint-plugins"
cat >"$tmp/.tflint.hcl" <<HCL
plugin "aws" {
  enabled = true
  version = "$(v tflint_ruleset_aws)"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
HCL
TFLINT_PLUGIN_DIR="$here/.tools/tflint-plugins" "$bin/tflint" --init --config="$tmp/.tflint.hcl" >/dev/null

# checkov is a Python tool: its own venv, exact version, so it cannot clash with the agent's deps.
ck="$(v checkov)"
py="$(command -v python3.12 || command -v python3.11 || command -v python3)"
"$py" -m venv "$here/.tools/checkov-venv"
"$here/.tools/checkov-venv/bin/pip" install -q "checkov==$ck"
ln -sf "../checkov-venv/bin/checkov" "$bin/checkov"

chmod +x "$bin"/*
echo "Installed into $bin:"
"$bin/terraform" version | head -1
TFLINT_PLUGIN_DIR="$here/.tools/tflint-plugins" "$bin/tflint" --version --config="$tmp/.tflint.hcl"
echo "gitleaks $("$bin/gitleaks" version)"
echo "checkov $("$bin/checkov" --version)"

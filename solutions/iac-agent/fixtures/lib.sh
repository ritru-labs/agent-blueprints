#!/usr/bin/env bash
# Shared helpers for fixture scripts. Fixtures simulate hand-built ("ClickOps")
# infrastructure, so they are created with the AWS CLI, never with Terraform.
set -euo pipefail

FIXTURE_TAG_KEY="iac-agent-fixture"
RUN_TAG_KEY="iac-agent-run"
FIXTURES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${FIXTURES_DIR}/out"

# Refuse to run anywhere except the agreed sandbox account and region.
guard_sandbox() {
  : "${SANDBOX_ACCOUNT_ID:?set SANDBOX_ACCOUNT_ID to the sandbox account ID}"
  : "${AWS_REGION:?set AWS_REGION}"
  local actual
  actual="$(aws sts get-caller-identity --query Account --output text)"
  if [[ "$actual" != "$SANDBOX_ACCOUNT_ID" ]]; then
    echo "Refusing: credentials are for account $actual, not sandbox $SANDBOX_ACCOUNT_ID" >&2
    exit 2
  fi
}

new_run_id() { date -u +%Y%m%dT%H%M%SZ; }

# tags <resource-type> <fixture> <run> <name>  -> value for --tag-specifications
tags() {
  printf 'ResourceType=%s,Tags=[{Key=Name,Value=%s},{Key=%s,Value=%s},{Key=%s,Value=%s}]' \
    "$1" "$4" "$FIXTURE_TAG_KEY" "$2" "$RUN_TAG_KEY" "$3"
}

# Manifest: what the agent must adopt or exclude, with the exact import ID.
manifest_init() { # fixture run
  mkdir -p "$OUT_DIR"
  MANIFEST="${OUT_DIR}/$1-$2.manifest.json"
  jq -n --arg f "$1" --arg r "$2" --arg region "$AWS_REGION" --arg acct "$SANDBOX_ACCOUNT_ID" \
    '{fixture:$f, run:$r, account:$acct, region:$region, resources:[]}' >"$MANIFEST"
}

# manifest_add <terraform_type> <import_id> <adopt|exclude> <reason>
manifest_add() {
  local tmp; tmp="$(mktemp)"
  jq --arg t "$1" --arg id "$2" --arg e "$3" --arg why "$4" \
    '.resources += [{terraform_type:$t, import_id:$id, expect:$e, reason:$why}]' \
    "$MANIFEST" >"$tmp" && mv "$tmp" "$MANIFEST"
}

first_two_azs() {
  aws ec2 describe-availability-zones --filters Name=state,Values=available \
    --query 'AvailabilityZones[0:2].ZoneName' --output text
}

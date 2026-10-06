#!/usr/bin/env bash
# Shared helpers for fixture scripts. Fixtures simulate hand-built ("ClickOps")
# infrastructure, so they are created with the AWS CLI, never with Terraform.
set -euo pipefail

FIXTURE_TAG_KEY="iac-agent-fixture"
RUN_TAG_KEY="iac-agent-run"
# IAM roles, policies, instance profiles and S3 buckets start with this, so
# teardown can find them; it still checks the fixture tag before deleting.
# shellcheck disable=SC2034  # used by the scripts that source this file
NAME_PREFIX="iacfx"
FIXTURES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${FIXTURE_OUT_DIR:-${FIXTURES_DIR}/out}"

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
lower() { tr '[:upper:]' '[:lower:]' <<<"$1"; }

# tags <resource-type> <fixture> <run> <name>  -> value for --tag-specifications
tags() {
  printf 'ResourceType=%s,Tags=[{Key=Name,Value=%s},{Key=%s,Value=%s},{Key=%s,Value=%s}]' \
    "$1" "$4" "$FIXTURE_TAG_KEY" "$2" "$RUN_TAG_KEY" "$3"
}

# tag_json <fixture> <run> <name>  -> JSON tag list for IAM, S3 and others
tag_json() {
  jq -nc --arg f "$1" --arg r "$2" --arg n "$3" --arg fk "$FIXTURE_TAG_KEY" --arg rk "$RUN_TAG_KEY" \
    '[{Key:"Name",Value:$n},{Key:$fk,Value:$f},{Key:$rk,Value:$r}]'
}

# Retries a command that can fail briefly while AWS settles (IAM propagation,
# ENIs released after termination). Fails after the last attempt.
retry() {
  local n
  for n in 1 2 3 4 5 6 7 8 9 10; do
    "$@" && return 0
    echo "retry $n/10: $*" >&2
    sleep "${FIXTURE_RETRY_SECONDS:-6}"
  done
  "$@"
}

# Manifest: what the agent must adopt or exclude, with the exact import ID,
# and the outcome the run must reach. Outcomes: pass (every gate passes),
# blocked, restart, hard_stop. Unless the outcome is pass, nothing may reach
# state; "adopt" then says how the resource must be classified.
manifest_init() { # fixture run
  mkdir -p "$OUT_DIR"
  MANIFEST="${OUT_DIR}/$1-$2.manifest.json"
  jq -n --arg f "$1" --arg r "$2" --arg region "$AWS_REGION" --arg acct "$SANDBOX_ACCOUNT_ID" \
    '{fixture:$f, run:$r, account:$acct, region:$region, expect:{outcome:"pass"}, resources:[]}' >"$MANIFEST"
}

# manifest_add <terraform_type> <import_id> <adopt|exclude> <reason>
manifest_add() {
  local tmp; tmp="$(mktemp)"
  jq --arg t "$1" --arg id "$2" --arg e "$3" --arg why "$4" \
    '.resources += [{terraform_type:$t, import_id:$id, expect:$e, reason:$why}]' \
    "$MANIFEST" >"$tmp" && mv "$tmp" "$MANIFEST"
}

# manifest_expect <outcome> <pipeline-step> <detail>
manifest_expect() {
  manifest_set expect "$(jq -n --arg o "$1" --arg s "$2" --arg d "$3" '{outcome:$o, step:$s, detail:$d}')"
}

# manifest_set <key> <json>
manifest_set() {
  local tmp; tmp="$(mktemp)"
  jq --arg k "$1" --argjson v "$2" '.[$k] = $v' "$MANIFEST" >"$tmp" && mv "$tmp" "$MANIFEST"
}

# AWS creates a main route table, default SG and default NACL with every VPC.
# V1 excludes default resources. Usage: manifest_vpc_defaults <vpc-id> [reason-suffix]
manifest_vpc_defaults() {
  local vpc="$1" why="${2:-}" main_rtb default_sg default_acl
  main_rtb="$(aws ec2 describe-route-tables --filters Name=vpc-id,Values="$vpc" Name=association.main,Values=true \
    --query 'RouteTables[0].RouteTableId' --output text)"
  default_sg="$(aws ec2 describe-security-groups --filters Name=vpc-id,Values="$vpc" Name=group-name,Values=default \
    --query 'SecurityGroups[0].GroupId' --output text)"
  default_acl="$(aws ec2 describe-network-acls --filters Name=vpc-id,Values="$vpc" Name=default,Values=true \
    --query 'NetworkAcls[0].NetworkAclId' --output text)"
  manifest_add aws_route_table "$main_rtb" exclude "AWS-created main route table${why}"
  manifest_add aws_security_group "$default_sg" exclude "AWS-created default security group${why}"
  manifest_add aws_network_acl "$default_acl" exclude "AWS-created default network ACL${why}"
}

# Rule IDs of a security group. Usage: sg_rule_ids <sg-id> <true|false (egress)>
sg_rule_ids() {
  aws ec2 describe-security-group-rules --filters Name=group-id,Values="$1" \
    --query "SecurityGroupRules[?IsEgress==\`$2\`].SecurityGroupRuleId" --output text
}

# authorize_rule <ingress|egress> <sg-id> <ip-permissions-json>  -> new rule ID
authorize_rule() {
  aws ec2 "authorize-security-group-$1" --group-id "$2" --ip-permissions "$3" \
    --query 'SecurityGroupRules[0].SecurityGroupRuleId' --output text
}

# create_bucket <name> <fixture> <run>. Buckets made today already get SSE-S3,
# Block Public Access and BucketOwnerEnforced from AWS without being asked.
create_bucket() {
  if [[ "$AWS_REGION" == us-east-1 ]]; then
    aws s3api create-bucket --bucket "$1" >/dev/null
  else
    aws s3api create-bucket --bucket "$1" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
  fi
  aws s3api wait bucket-exists --bucket "$1"
  aws s3api put-bucket-tagging --bucket "$1" --tagging "{\"TagSet\":$(tag_json "$2" "$3" "$1")}"
}

first_two_azs() {
  aws ec2 describe-availability-zones --filters Name=state,Values=available \
    --query 'AvailabilityZones[0:2].ZoneName' --output text
}

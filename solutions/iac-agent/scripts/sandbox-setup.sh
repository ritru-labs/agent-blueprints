#!/usr/bin/env bash
# One-time sandbox setup for running the agent on fixtures: a KMS key and an
# encrypted, versioned, private S3 bucket for Terraform state, and the two agent
# roles (scanner, importer) from iam/, trusted only by the identity that runs
# this script, with an external ID. Idempotent: re-running updates in place.
# Writes fixtures/out/sandbox.env for the harness.
#
# Usage (sandbox admin credentials):
#   SANDBOX_ACCOUNT_ID=... AWS_REGION=... scripts/sandbox-setup.sh
# Then prove the roles cannot write:
#   source fixtures/out/sandbox.env && scripts/verify-iam-cannot-write.sh
set -euo pipefail
# shellcheck source=fixtures/lib.sh
source "$(dirname "$0")/../fixtures/lib.sh"
here="$(cd "$(dirname "$0")/.." && pwd)"

guard_sandbox
acct="$SANDBOX_ACCOUNT_ID"
bucket="iac-agent-state-${acct}-${AWS_REGION}"
alias="alias/iac-agent-state"
env_file="$OUT_DIR/sandbox.env"
mkdir -p "$OUT_DIR"

# Keep the external ID stable across re-runs.
external_id="$( (grep -s '^export EXTERNAL_ID=' "$env_file" || true) | cut -d= -f2)"
[[ -n "$external_id" ]] || external_id="$(openssl rand -hex 16)"

# The runner identity: an assumed role is trusted as its role, not the session.
caller="$(aws sts get-caller-identity --query Arn --output text)"
if [[ "$caller" == arn:aws:sts::*:assumed-role/* ]]; then
  role_name="$(cut -d/ -f2 <<<"$caller")"
  caller="$(aws iam get-role --role-name "$role_name" --query Role.Arn --output text)"
fi
echo "Agent roles will trust: $caller"

# --- KMS key for state ---------------------------------------------------------
key_arn="$(aws kms describe-key --key-id "$alias" --query KeyMetadata.Arn --output text 2>/dev/null || true)"
if [[ -z "$key_arn" || "$key_arn" == None ]]; then
  key_arn="$(aws kms create-key --description "iac-agent Terraform state" \
    --tags TagKey=iac-agent,TagValue=state --query KeyMetadata.Arn --output text)"
  aws kms create-alias --alias-name "$alias" --target-key-id "$key_arn"
  aws kms enable-key-rotation --key-id "$key_arn"
fi

# --- State bucket ----------------------------------------------------------------
if ! aws s3api head-bucket --bucket "$bucket" 2>/dev/null; then
  if [[ "$AWS_REGION" == us-east-1 ]]; then
    aws s3api create-bucket --bucket "$bucket" >/dev/null
  else
    aws s3api create-bucket --bucket "$bucket" --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
  fi
  aws s3api wait bucket-exists --bucket "$bucket"
fi
aws s3api put-bucket-versioning --bucket "$bucket" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$bucket" --server-side-encryption-configuration "$(jq -nc --arg k "$key_arn" \
  '{Rules:[{ApplyServerSideEncryptionByDefault:{SSEAlgorithm:"aws:kms",KMSMasterKeyID:$k},BucketKeyEnabled:true}]}')"
aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-policy --bucket "$bucket" --policy "$(jq -nc --arg b "arn:aws:s3:::$bucket" \
  '{Version:"2012-10-17",Statement:[{Sid:"DenyInsecureTransport",Effect:"Deny",Principal:"*",Action:"s3:*",
    Resource:[$b,($b+"/*")],Condition:{Bool:{"aws:SecureTransport":"false"}}}]}')"

# --- Agent roles -------------------------------------------------------------------
trust="$(sed -e "s|REPLACE_TRUSTED_PRINCIPAL_ARN|$caller|" -e "s|REPLACE_EXTERNAL_ID|$external_id|" \
  "$here/iam/trust-policy.template.json")"
role() { # name policy-json
  if aws iam get-role --role-name "$1" >/dev/null 2>&1; then
    aws iam update-assume-role-policy --role-name "$1" --policy-document "$trust"
  else
    aws iam create-role --role-name "$1" --assume-role-policy-document "$trust" \
      --description "iac-agent $1 (read-only by design)" --tags Key=iac-agent,Value=role >/dev/null
  fi
  aws iam put-role-policy --role-name "$1" --policy-name "$1" --policy-document "$2"
  aws iam get-role --role-name "$1" --query Role.Arn --output text
}
scanner_arn="$(role iac-agent-scanner "$(cat "$here/iam/scanner-policy.json")")"
importer_arn="$(role iac-agent-importer "$(sed -e "s|REPLACE_STATE_BUCKET|$bucket|g" \
  -e "s|REPLACE_STATE_KMS_KEY_ARN|$key_arn|g" "$here/iam/importer-policy.template.json")")"

cat >"$env_file" <<ENV
# Written by scripts/sandbox-setup.sh. Not secret: ARNs and an external ID only.
export SANDBOX_ACCOUNT_ID=$acct
export AWS_REGION=$AWS_REGION
export SCANNER_ROLE_ARN=$scanner_arn
export IMPORTER_ROLE_ARN=$importer_arn
export EXTERNAL_ID=$external_id
export STATE_BUCKET=$bucket
export STATE_KMS_KEY_ARN=$key_arn
ENV
echo "Sandbox ready. Settings: $env_file"

#!/usr/bin/env bash
# Live proof, via the AWS IAM policy simulator, that the scanner and importer
# policies deny every write we care about and allow the reads adoption needs.
# Calls only iam:SimulateCustomPolicy; creates nothing in the account.
#
# Usage: STATE_BUCKET=my-tf-state STATE_KMS_KEY_ARN=arn:aws:kms:... \
#        scripts/verify-iam-cannot-write.sh
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
: "${STATE_BUCKET:?set STATE_BUCKET}"
: "${STATE_KMS_KEY_ARN:?set STATE_KMS_KEY_ARN}"

scanner="$(cat "$here/iam/scanner-policy.json")"
importer="$(sed -e "s|REPLACE_STATE_BUCKET|${STATE_BUCKET}|g" \
                -e "s|REPLACE_STATE_KMS_KEY_ARN|${STATE_KMS_KEY_ARN}|g" \
                "$here/iam/importer-policy.template.json")"

writes=(
  ec2:RunInstances ec2:TerminateInstances ec2:CreateVpc ec2:DeleteVpc
  ec2:ModifyVpcAttribute ec2:AuthorizeSecurityGroupIngress ec2:CreateTags
  ec2:DeleteTags ec2:GetPasswordData iam:CreateRole iam:PutRolePolicy
  iam:AttachRolePolicy iam:PassRole iam:CreateAccessKey s3:CreateBucket
  s3:DeleteBucket s3:PutBucketPolicy s3:PutBucketVersioning
  secretsmanager:GetSecretValue ssm:GetParameter cloudformation:DeleteStack
)
reads=(
  ec2:DescribeVpcs ec2:DescribeSubnets ec2:DescribeInstanceAttribute
  iam:GetRole iam:ListAttachedRolePolicies s3:GetBucketPolicy s3:ListBucket
)

fail=0
check() { # name policy expected actions...
  local name="$1" policy="$2" expected="$3"; shift 3
  local out
  out="$(aws iam simulate-custom-policy --policy-input-list "$policy" \
        --action-names "$@" --query 'EvaluationResults[].[EvalActionName,EvalDecision]' \
        --output text)"
  while read -r action decision; do
    if [[ "$expected" == deny && "$decision" == allowed ]] || \
       [[ "$expected" == allow && "$decision" != allowed ]]; then
      echo "FAIL $name $action -> $decision (expected $expected)"; fail=1
    else
      echo "ok   $name $action -> $decision"
    fi
  done <<<"$out"
}

check scanner  "$scanner"  deny  "${writes[@]}"
check importer "$importer" deny  "${writes[@]}"
check scanner  "$scanner"  allow "${reads[@]}"
check importer "$importer" allow "${reads[@]}"
check scanner  "$scanner"  deny  s3:GetObject s3:PutObject

# Importer may touch state objects only inside the state bucket.
out="$(aws iam simulate-custom-policy --policy-input-list "$importer" \
      --action-names s3:PutObject s3:GetObject \
      --resource-arns "arn:aws:s3:::${STATE_BUCKET}/x.tfstate" \
      --query 'EvaluationResults[].EvalDecision' --output text)"
[[ "$out" == $'allowed\tallowed' ]] && echo "ok   importer state bucket rw" \
  || { echo "FAIL importer state bucket rw -> $out"; fail=1; }
out="$(aws iam simulate-custom-policy --policy-input-list "$importer" \
      --action-names s3:GetObject s3:PutObject \
      --resource-arns "arn:aws:s3:::some-other-bucket/data.csv" \
      --query 'EvaluationResults[].EvalDecision' --output text)"
[[ "$out" != *allowed* ]] && echo "ok   importer other bucket denied" \
  || { echo "FAIL importer other bucket -> $out"; fail=1; }

exit "$fail"

#!/usr/bin/env bash
# F5 permission gap: an S3 bucket exists, but the scanner policy written next
# to the manifest has every S3 read removed. The test harness attaches it to
# the scanner (as the role policy or as an STS session policy).
# Expected: discovery hits AccessDenied for S3, coverage is marked incomplete
# and the run blocks. Nothing reaches state.
set -euo pipefail
# shellcheck source=fixtures/lib.sh
source "$(dirname "$0")/../lib.sh"

guard_sandbox
F=F5 RUN="$(new_run_id)"
manifest_init "$F" "$RUN"

bucket="${NAME_PREFIX}-$(lower "$F-$RUN")-${SANDBOX_ACCOUNT_ID}"
create_bucket "$bucket" "$F" "$RUN"
manifest_add aws_s3_bucket "$bucket" adopt "hidden from the scanner; the run must block before adoption"

policy="${OUT_DIR}/$F-$RUN.scanner-policy.json"
jq '.Statement |= map(if .Effect == "Allow" then .Action |= map(select(startswith("s3:") | not)) else . end)' \
  "$FIXTURES_DIR/../iam/scanner-policy.json" >"$policy"
manifest_set scanner_policy "$(jq -n --arg f "${policy##*/}" '{file:$f, removed_service:"s3"}')"  # next to the manifest
manifest_expect blocked discover "AccessDenied on s3 reads: coverage incomplete for aws_s3_bucket*"

echo "F5 created (run $RUN). Manifest: $MANIFEST"

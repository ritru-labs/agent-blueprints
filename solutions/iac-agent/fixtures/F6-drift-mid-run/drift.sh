#!/usr/bin/env bash
# Applies F6's drift: changes the tag recorded in the run's manifest. Touches
# only a resource that carries this run's fixture tag.
# Usage: fixtures/F6-drift-mid-run/drift.sh <run-id>
set -euo pipefail
# shellcheck source=fixtures/lib.sh
source "$(dirname "$0")/../lib.sh"

run="${1:?usage: drift.sh <run-id>}"
guard_sandbox
MANIFEST="${OUT_DIR}/F6-${run}.manifest.json"
id="$(jq -r .drift.resource_id "$MANIFEST")"
tag="$(jq -r .drift.tag "$MANIFEST")"
to="$(jq -r .drift.to "$MANIFEST")"

owner_run="$(aws ec2 describe-tags --filters Name=resource-id,Values="$id" Name=key,Values="$RUN_TAG_KEY" \
  --query 'Tags[0].Value' --output text)"
if [[ "$owner_run" != "$run" ]]; then
  echo "Refusing: $id is not tagged $RUN_TAG_KEY=$run" >&2
  exit 2
fi
aws ec2 create-tags --resources "$id" --tags "Key=$tag,Value=$to"
echo "Drifted $id: $tag=$to"

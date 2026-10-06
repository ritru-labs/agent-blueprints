#!/usr/bin/env bash
# F6 drift mid-run: a small VPC. The harness runs drift.sh <run> after the scan
# and before import; it changes the VPC's Owner tag from team-a to team-b.
# Expected: the re-scan fingerprint at approval differs, the run goes back to
# discovery, and the import uses the new tag. No stale import.
set -euo pipefail
# shellcheck source=fixtures/lib.sh
source "$(dirname "$0")/../lib.sh"

guard_sandbox
F=F6 RUN="$(new_run_id)"
manifest_init "$F" "$RUN"
read -r AZ_A _ <<<"$(first_two_azs)"

vpc="$(aws ec2 create-vpc --cidr-block 10.46.0.0/16 \
  --tag-specifications "$(tags vpc "$F" "$RUN" f6-vpc)" --query Vpc.VpcId --output text)"
aws ec2 wait vpc-available --vpc-ids "$vpc"
aws ec2 create-tags --resources "$vpc" --tags Key=Owner,Value=team-a
manifest_add aws_vpc "$vpc" adopt "created by fixture; drifts mid-run"
sub="$(aws ec2 create-subnet --vpc-id "$vpc" --cidr-block 10.46.1.0/24 --availability-zone "$AZ_A" \
  --tag-specifications "$(tags subnet "$F" "$RUN" f6-subnet)" --query Subnet.SubnetId --output text)"
manifest_add aws_subnet "$sub" adopt "created by fixture"
manifest_vpc_defaults "$vpc"

manifest_set drift "$(jq -n --arg id "$vpc" '{resource_id:$id, tag:"Owner", from:"team-a", to:"team-b"}')"
manifest_expect restart approve "re-scan fingerprint changed (Owner tag on $vpc); back to discover, then adopt"

echo "F6 created (run $RUN). Manifest: $MANIFEST"
echo "Between scan and import run: $(dirname "$0")/drift.sh $RUN"

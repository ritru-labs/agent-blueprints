#!/usr/bin/env bash
# F7 forced replacement: a VPC with one subnet in the first AZ. The harness
# mutates the generated aws_subnet to the second AZ, as recorded in the
# manifest. availability_zone forces replacement.
# Expected: the plan gate sees "replace" and hard-stops. No apply, empty state.
set -euo pipefail
# shellcheck source=fixtures/lib.sh
source "$(dirname "$0")/../lib.sh"

guard_sandbox
F=F7 RUN="$(new_run_id)"
manifest_init "$F" "$RUN"
read -r AZ_A AZ_B <<<"$(first_two_azs)"

vpc="$(aws ec2 create-vpc --cidr-block 10.47.0.0/16 \
  --tag-specifications "$(tags vpc "$F" "$RUN" f7-vpc)" --query Vpc.VpcId --output text)"
aws ec2 wait vpc-available --vpc-ids "$vpc"
manifest_add aws_vpc "$vpc" adopt "created by fixture"
sub="$(aws ec2 create-subnet --vpc-id "$vpc" --cidr-block 10.47.1.0/24 --availability-zone "$AZ_A" \
  --tag-specifications "$(tags subnet "$F" "$RUN" f7-subnet)" --query Subnet.SubnetId --output text)"
manifest_add aws_subnet "$sub" adopt "created by fixture; its generated code is mutated"
manifest_vpc_defaults "$vpc"

manifest_set mutation "$(jq -n --arg id "$sub" --arg a "$AZ_A" --arg b "$AZ_B" \
  '{terraform_type:"aws_subnet", import_id:$id, attribute:"availability_zone", from:$a, to:$b}')"
manifest_expect hard_stop plan "replace on aws_subnet $sub (availability_zone forces replacement)"

echo "F7 created (run $RUN). Manifest: $MANIFEST"

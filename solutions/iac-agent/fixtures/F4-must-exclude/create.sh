#!/usr/bin/env bash
# F4 must exclude: a CloudFormation stack (VPC, subnet, SG, launch template),
# an Auto Scaling group instance launched from it, the default VPC, and the
# Auto Scaling service-linked role.
# Expected: every resource excluded with a reason; nothing adopted.
# Cost while it runs: one t3.micro.
set -euo pipefail
# shellcheck source=fixtures/lib.sh
source "$(dirname "$0")/../lib.sh"

guard_sandbox
F=F4 RUN="$(new_run_id)"
manifest_init "$F" "$RUN"

# --- CloudFormation-owned resources ------------------------------------------
stack="${NAME_PREFIX}-$F-$RUN"
aws cloudformation create-stack --stack-name "$stack" \
  --template-body "file://$(cd "$(dirname "$0")" && pwd)/stack.yaml" \
  --tags "$(tag_json "$F" "$RUN" "$stack")" >/dev/null
aws cloudformation wait stack-create-complete --stack-name "$stack"
physical_id() {
  aws cloudformation describe-stack-resource --stack-name "$stack" --logical-resource-id "$1" \
    --query StackResourceDetail.PhysicalResourceId --output text
}
cfn_vpc="$(physical_id Vpc)"
cfn_subnet="$(physical_id Subnet)"
cfn_sg="$(physical_id SecurityGroup)"
cfn_lt="$(physical_id LaunchTemplate)"
why="owned by CloudFormation stack $stack"
manifest_add aws_vpc "$cfn_vpc" exclude "$why"
manifest_add aws_subnet "$cfn_subnet" exclude "$why"
manifest_add aws_security_group "$cfn_sg" exclude "$why"
manifest_add aws_launch_template "$cfn_lt" exclude "$why"
manifest_add aws_vpc_security_group_egress_rule "$(sg_rule_ids "$cfn_sg" true)" exclude \
  "default egress rule of a CloudFormation-owned group"
manifest_vpc_defaults "$cfn_vpc" " (VPC owned by CloudFormation)"

# --- Auto Scaling group built by hand from the stack's launch template --------
asg="${NAME_PREFIX}-$F-$RUN-asg"
aws autoscaling create-auto-scaling-group --auto-scaling-group-name "$asg" \
  --launch-template "LaunchTemplateId=$cfn_lt,Version=\$Default" \
  --min-size 1 --max-size 1 --desired-capacity 1 --vpc-zone-identifier "$cfn_subnet" \
  --tags "$(tag_json "$F" "$RUN" "$asg" | jq -c --arg a "$asg" \
    'map(. + {ResourceId:$a, ResourceType:"auto-scaling-group", PropagateAtLaunch:true})')"
instance=None
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  instance="$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$asg" \
    --query 'AutoScalingGroups[0].Instances[0].InstanceId' --output text)"
  [[ "$instance" == i-* ]] && break
  sleep "${FIXTURE_RETRY_SECONDS:-10}"
done
[[ "$instance" == i-* ]] || { echo "Auto Scaling group $asg launched no instance" >&2; exit 1; }
aws ec2 wait instance-running --instance-ids "$instance"
root_vol="$(aws ec2 describe-instances --instance-ids "$instance" \
  --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text)"
eni="$(aws ec2 describe-instances --instance-ids "$instance" \
  --query 'Reservations[0].Instances[0].NetworkInterfaces[0].NetworkInterfaceId' --output text)"
manifest_add aws_autoscaling_group "$asg" exclude "Auto Scaling is excluded in V1"
manifest_add aws_instance "$instance" exclude "launched by Auto Scaling group $asg"
manifest_add aws_ebs_volume "$root_vol" exclude "root volume of an Auto Scaling instance"
manifest_add aws_network_interface "$eni" exclude "ENI of an Auto Scaling instance"

# AWS creates this role with the first Auto Scaling group in the account. It may
# be shared with real workloads, so teardown never deletes it.
slr=AWSServiceRoleForAutoScaling
aws iam get-role --role-name "$slr" >/dev/null
manifest_add aws_iam_role "$slr" exclude "service-linked role (/aws-service-role/)"

# --- Default VPC ---------------------------------------------------------------
default_vpc="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
if [[ "$default_vpc" == None || -z "$default_vpc" ]]; then
  # None in this region: create one and tag it, so teardown removes it again.
  # A default VPC that already existed is never tagged, so teardown leaves it alone.
  default_vpc="$(aws ec2 create-default-vpc --query Vpc.VpcId --output text)"
  aws ec2 wait vpc-available --vpc-ids "$default_vpc"
  aws ec2 create-tags --resources "$default_vpc" --tags "$(tag_json "$F" "$RUN" f4-default-vpc)"
fi
manifest_add aws_vpc "$default_vpc" exclude "default VPC"
for s in $(aws ec2 describe-subnets --filters Name=vpc-id,Values="$default_vpc" \
    --query 'Subnets[].SubnetId' --output text); do
  manifest_add aws_subnet "$s" exclude "default subnet of the default VPC"
done
for igw in $(aws ec2 describe-internet-gateways --filters Name=attachment.vpc-id,Values="$default_vpc" \
    --query 'InternetGateways[].InternetGatewayId' --output text); do
  manifest_add aws_internet_gateway "$igw" exclude "internet gateway of the default VPC"
done
manifest_vpc_defaults "$default_vpc" " of the default VPC"

echo "F4 created (run $RUN). Manifest: $MANIFEST"

#!/usr/bin/env bash
# Deletes fixture network resources by tag. Only resources carrying the
# fixture tag (and optionally a run tag) are touched, and only in the sandbox.
#
# Usage: fixtures/teardown.sh [run-id]
# Covers the network resources F1 creates. Extend it with each new fixture.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

guard_sandbox
filter="Name=tag-key,Values=${FIXTURE_TAG_KEY}"
[[ $# -ge 1 ]] && filter="Name=tag:${RUN_TAG_KEY},Values=$1"

vpcs="$(aws ec2 describe-vpcs --filters "$filter" --query 'Vpcs[].VpcId' --output text)"
for vpc in $vpcs; do
  echo "Tearing down $vpc"
  for rtb in $(aws ec2 describe-route-tables --filters Name=vpc-id,Values="$vpc" \
      --query 'RouteTables[?!(Associations[?Main])].RouteTableId' --output text); do
    for assoc in $(aws ec2 describe-route-tables --route-table-ids "$rtb" \
        --query 'RouteTables[0].Associations[?!Main].RouteTableAssociationId' --output text); do
      aws ec2 disassociate-route-table --association-id "$assoc"
    done
    aws ec2 delete-route-table --route-table-id "$rtb"
  done
  for igw in $(aws ec2 describe-internet-gateways --filters Name=attachment.vpc-id,Values="$vpc" \
      --query 'InternetGateways[].InternetGatewayId' --output text); do
    aws ec2 detach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$vpc"
    aws ec2 delete-internet-gateway --internet-gateway-id "$igw"
  done
  for sub in $(aws ec2 describe-subnets --filters Name=vpc-id,Values="$vpc" \
      --query 'Subnets[].SubnetId' --output text); do
    aws ec2 delete-subnet --subnet-id "$sub"
  done
  aws ec2 delete-vpc --vpc-id "$vpc"
done
echo "Teardown done."

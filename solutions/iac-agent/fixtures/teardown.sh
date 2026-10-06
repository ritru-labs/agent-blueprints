#!/usr/bin/env bash
# Deletes everything fixtures F1-F7 create, found by the fixture tag (and, with
# a run ID, the run tag). IAM and S3 resources must also start with the fixture
# name prefix; their tags are checked before anything is deleted. Only runs in
# the sandbox.
#
# Never touched: CloudFormation-owned resources (their stack is deleted
# instead), service-linked roles, a default VPC the fixtures did not create,
# and S3 object contents (a non-empty bucket makes teardown fail, by design).
#
# Usage: fixtures/teardown.sh [run-id]
set -euo pipefail
# shellcheck source=fixtures/lib.sh
source "$(dirname "$0")/lib.sh"

guard_sandbox
run="${1:-}"
filter="Name=tag-key,Values=${FIXTURE_TAG_KEY}"
[[ -n "$run" ]] && filter="Name=tag:${RUN_TAG_KEY},Values=$run"

# tagged <json-tag-list>: true if the tags mark a fixture resource (of this run).
tagged() {
  jq -e --arg k "$FIXTURE_TAG_KEY" --arg rk "$RUN_TAG_KEY" --arg run "$run" \
    'any(.[]; .Key == $k) and ($run == "" or any(.[]; .Key == $rk and .Value == $run))' <<<"$1" >/dev/null
}

# wait_gone <command...>: polls until the command prints nothing.
wait_gone() {
  local _
  for _ in $(seq 60); do
    [[ -z "$("$@")" ]] && return 0
    sleep "${FIXTURE_RETRY_SECONDS:-10}"
  done
  echo "Timed out waiting for: $*" >&2
  return 1
}

# --- Auto Scaling groups (F4): first, so they stop replacing instances -------
asgs="$(aws autoscaling describe-auto-scaling-groups --filters "$filter" \
  --query 'AutoScalingGroups[].AutoScalingGroupName' --output text)"
for asg in $asgs; do
  echo "Deleting Auto Scaling group $asg"
  aws autoscaling delete-auto-scaling-group --auto-scaling-group-name "$asg" --force-delete
done
for asg in $asgs; do
  wait_gone aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$asg" \
    --query 'AutoScalingGroups[].AutoScalingGroupName' --output text
done

# --- CloudFormation stacks (F4) ----------------------------------------------
stack_match="Key=='${FIXTURE_TAG_KEY}'"
[[ -n "$run" ]] && stack_match="Key=='${RUN_TAG_KEY}' && Value=='${run}'"
delete_stack() {
  aws cloudformation delete-stack --stack-name "$1"
  aws cloudformation wait stack-delete-complete --stack-name "$1"
}
for stack in $(aws cloudformation describe-stacks \
    --query "Stacks[?Tags[?${stack_match}]].StackName" --output text); do
  echo "Deleting stack $stack"
  retry delete_stack "$stack"  # can fail once while ENIs of terminated instances linger
done

# --- EC2 instances and volumes (F2) ------------------------------------------
instances="$(aws ec2 describe-instances --filters "$filter" \
  Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
if [[ -n "$instances" ]]; then
  echo "Terminating $instances"
  # shellcheck disable=SC2086  # IDs are split into separate arguments on purpose
  aws ec2 terminate-instances --instance-ids $instances >/dev/null
  # shellcheck disable=SC2086
  aws ec2 wait instance-terminated --instance-ids $instances
fi
# Root volumes go with their instance; extra volumes detach and are deleted here.
for vol in $(aws ec2 describe-volumes --filters "$filter" Name=status,Values=creating,available,in-use \
    --query 'Volumes[].VolumeId' --output text); do
  aws ec2 wait volume-available --volume-ids "$vol"
  aws ec2 delete-volume --volume-id "$vol"
done

# --- NAT gateways, then their Elastic IPs (F2) --------------------------------
nats="$(aws ec2 describe-nat-gateways --filter "$filter" Name=state,Values=pending,available \
  --query 'NatGateways[].NatGatewayId' --output text)"
for nat in $nats; do
  echo "Deleting NAT gateway $nat"
  aws ec2 delete-nat-gateway --nat-gateway-id "$nat" >/dev/null
done
if [[ -n "$nats" ]]; then
  # shellcheck disable=SC2086
  aws ec2 wait nat-gateway-deleted --nat-gateway-ids $nats
fi
for alloc in $(aws ec2 describe-addresses --filters "$filter" --query 'Addresses[].AllocationId' --output text); do
  retry aws ec2 release-address --allocation-id "$alloc"
done

# --- VPCs and what is inside them (F1-F7) ------------------------------------
# CloudFormation-owned VPCs are skipped: their stack deletes them.
vpcs="$(aws ec2 describe-vpcs --filters "$filter" \
  --query "Vpcs[?!(Tags[?starts_with(Key, 'aws:cloudformation:')])].VpcId" --output text)"
for vpc in $vpcs; do
  echo "Tearing down $vpc"
  # Revoke every rule first: groups that reference each other cannot be deleted.
  sgs="$(aws ec2 describe-security-groups --filters Name=vpc-id,Values="$vpc" \
    --query "SecurityGroups[?GroupName!='default'].GroupId" --output text)"
  for sg in $sgs; do
    for direction in ingress egress; do
      ids="$(sg_rule_ids "$sg" "$([[ $direction == egress ]] && echo true || echo false)")"
      if [[ -n "$ids" ]]; then
        # shellcheck disable=SC2086
        aws ec2 "revoke-security-group-$direction" --group-id "$sg" --security-group-rule-ids $ids >/dev/null
      fi
    done
  done
  for sg in $sgs; do retry aws ec2 delete-security-group --group-id "$sg"; done

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
    retry aws ec2 detach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$vpc"
    aws ec2 delete-internet-gateway --internet-gateway-id "$igw"
  done
  for sub in $(aws ec2 describe-subnets --filters Name=vpc-id,Values="$vpc" \
      --query 'Subnets[].SubnetId' --output text); do
    retry aws ec2 delete-subnet --subnet-id "$sub"
  done
  retry aws ec2 delete-vpc --vpc-id "$vpc"
done

# --- IAM (F2, F3): instance profiles, then roles, then policies ---------------
for profile in $(aws iam list-instance-profiles \
    --query "InstanceProfiles[?starts_with(InstanceProfileName, '${NAME_PREFIX}-')].InstanceProfileName" --output text); do
  tagged "$(aws iam list-instance-profile-tags --instance-profile-name "$profile" --query Tags --output json)" || continue
  echo "Deleting instance profile $profile"
  for role in $(aws iam get-instance-profile --instance-profile-name "$profile" \
      --query 'InstanceProfile.Roles[].RoleName' --output text); do
    aws iam remove-role-from-instance-profile --instance-profile-name "$profile" --role-name "$role"
  done
  aws iam delete-instance-profile --instance-profile-name "$profile"
done
for role in $(aws iam list-roles --query "Roles[?starts_with(RoleName, '${NAME_PREFIX}-')].RoleName" --output text); do
  tagged "$(aws iam list-role-tags --role-name "$role" --query Tags --output json)" || continue
  echo "Deleting role $role"
  for arn in $(aws iam list-attached-role-policies --role-name "$role" \
      --query 'AttachedPolicies[].PolicyArn' --output text); do
    aws iam detach-role-policy --role-name "$role" --policy-arn "$arn"
  done
  for name in $(aws iam list-role-policies --role-name "$role" --query PolicyNames --output text); do
    aws iam delete-role-policy --role-name "$role" --policy-name "$name"
  done
  aws iam delete-role --role-name "$role"
done
for arn in $(aws iam list-policies --scope Local \
    --query "Policies[?starts_with(PolicyName, '${NAME_PREFIX}-')].Arn" --output text); do
  tagged "$(aws iam list-policy-tags --policy-arn "$arn" --query Tags --output json)" || continue
  echo "Deleting policy $arn"
  for v in $(aws iam list-policy-versions --policy-arn "$arn" \
      --query 'Versions[?!IsDefaultVersion].VersionId' --output text); do
    aws iam delete-policy-version --policy-arn "$arn" --version-id "$v"
  done
  aws iam delete-policy --policy-arn "$arn"
done

# --- S3 buckets (F2, F3, F5). Fixtures write no objects. ----------------------
for bucket in $(aws s3api list-buckets \
    --query "Buckets[?starts_with(Name, '${NAME_PREFIX}-')].Name" --output text); do
  tagged "$(aws s3api get-bucket-tagging --bucket "$bucket" --query TagSet --output json 2>/dev/null || echo '[]')" || continue
  echo "Deleting bucket $bucket"
  aws s3api delete-bucket --bucket "$bucket"
done

echo "Teardown done."

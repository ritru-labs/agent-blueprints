#!/usr/bin/env bash
# F2 typical web stack: VPC with public and private subnets, NAT gateway + EIP,
# security groups with separate rules, IAM role + instance profile, an EC2
# instance with an extra EBS volume, and an S3 bucket with versioning,
# encryption, policy and lifecycle.
# Expected: every created resource adopted at 0 changes. AWS-created defaults,
# the instance's root volume and ENIs, and AWS-managed policies are excluded.
# Cost while it runs: one NAT gateway, one t3.micro, 8 GiB gp3.
set -euo pipefail
# shellcheck source=fixtures/lib.sh
source "$(dirname "$0")/../lib.sh"

guard_sandbox
F=F2 RUN="$(new_run_id)"
manifest_init "$F" "$RUN"
read -r AZ_A AZ_B <<<"$(first_two_azs)"
P="${NAME_PREFIX}-$(lower "$F-$RUN")"
created="created by fixture"

# --- Network -----------------------------------------------------------------
vpc="$(aws ec2 create-vpc --cidr-block 10.43.0.0/16 \
  --tag-specifications "$(tags vpc "$F" "$RUN" f2-vpc)" --query Vpc.VpcId --output text)"
aws ec2 wait vpc-available --vpc-ids "$vpc"
aws ec2 modify-vpc-attribute --vpc-id "$vpc" --enable-dns-hostnames '{"Value":true}'
manifest_add aws_vpc "$vpc" adopt "$created"

subnet() { # cidr az name
  aws ec2 create-subnet --vpc-id "$vpc" --cidr-block "$1" --availability-zone "$2" \
    --tag-specifications "$(tags subnet "$F" "$RUN" "$3")" --query Subnet.SubnetId --output text
}
pub_a="$(subnet 10.43.1.0/24 "$AZ_A" f2-public-a)"
pub_b="$(subnet 10.43.2.0/24 "$AZ_B" f2-public-b)"
priv_a="$(subnet 10.43.11.0/24 "$AZ_A" f2-private-a)"
priv_b="$(subnet 10.43.12.0/24 "$AZ_B" f2-private-b)"
for s in "$pub_a" "$pub_b"; do aws ec2 modify-subnet-attribute --subnet-id "$s" --map-public-ip-on-launch; done
for s in "$pub_a" "$pub_b" "$priv_a" "$priv_b"; do manifest_add aws_subnet "$s" adopt "$created"; done

igw="$(aws ec2 create-internet-gateway --tag-specifications "$(tags internet-gateway "$F" "$RUN" f2-igw)" \
  --query InternetGateway.InternetGatewayId --output text)"
aws ec2 attach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$vpc"
manifest_add aws_internet_gateway "$igw" adopt "$created"

eip="$(aws ec2 allocate-address --domain vpc --tag-specifications "$(tags elastic-ip "$F" "$RUN" f2-nat-eip)" \
  --query AllocationId --output text)"
manifest_add aws_eip "$eip" adopt "$created"
nat="$(aws ec2 create-nat-gateway --subnet-id "$pub_a" --allocation-id "$eip" \
  --tag-specifications "$(tags natgateway "$F" "$RUN" f2-nat)" --query NatGateway.NatGatewayId --output text)"
aws ec2 wait nat-gateway-available --nat-gateway-ids "$nat"
manifest_add aws_nat_gateway "$nat" adopt "$created"
nat_eni="$(aws ec2 describe-nat-gateways --nat-gateway-ids "$nat" \
  --query 'NatGateways[0].NatGatewayAddresses[0].NetworkInterfaceId' --output text)"
manifest_add aws_network_interface "$nat_eni" exclude "service-created ENI of NAT gateway $nat"

route_table() { # name target-flag target-id subnets...
  local name="$1" flag="$2" target="$3" rtb s; shift 3
  rtb="$(aws ec2 create-route-table --vpc-id "$vpc" --tag-specifications "$(tags route-table "$F" "$RUN" "$name")" \
    --query RouteTable.RouteTableId --output text)"
  aws ec2 create-route --route-table-id "$rtb" --destination-cidr-block 0.0.0.0/0 "$flag" "$target" >/dev/null
  manifest_add aws_route_table "$rtb" adopt "$created"
  manifest_add aws_route "${rtb}_0.0.0.0/0" adopt "$created"
  for s in "$@"; do
    aws ec2 associate-route-table --route-table-id "$rtb" --subnet-id "$s" >/dev/null
    manifest_add aws_route_table_association "${s}/${rtb}" adopt "$created"
  done
}
route_table f2-public-rt --gateway-id "$igw" "$pub_a" "$pub_b"
route_table f2-private-rt --nat-gateway-id "$nat" "$priv_a" "$priv_b"

manifest_vpc_defaults "$vpc"

# --- Security groups (one rule per resource) ---------------------------------
security_group() { # name description
  aws ec2 create-security-group --vpc-id "$vpc" --group-name "$1" --description "$2" \
    --tag-specifications "$(tags security-group "$F" "$RUN" "$1")" --query GroupId --output text
}
web_sg="$(security_group f2-web "Web tier")"
app_sg="$(security_group f2-app "App tier")"
for sg in "$web_sg" "$app_sg"; do
  manifest_add aws_security_group "$sg" adopt "$created"
  # AWS adds an allow-all egress rule to every new group. It is real config on a
  # customer group and looks the same as a hand-added rule, so it is adopted.
  manifest_add aws_vpc_security_group_egress_rule "$(sg_rule_ids "$sg" true)" adopt "AWS default egress rule on a fixture group"
done
for port in 80 443; do
  rule="$(authorize_rule ingress "$web_sg" "[{\"IpProtocol\":\"tcp\",\"FromPort\":$port,\"ToPort\":$port,\"IpRanges\":[{\"CidrIp\":\"0.0.0.0/0\"}]}]")"
  manifest_add aws_vpc_security_group_ingress_rule "$rule" adopt "$created"
done
rule="$(authorize_rule ingress "$app_sg" '[{"IpProtocol":"tcp","FromPort":8080,"ToPort":8080,"IpRanges":[{"CidrIp":"10.43.0.0/16","Description":"app from VPC"}]}]')"
manifest_add aws_vpc_security_group_ingress_rule "$rule" adopt "$created"

# --- IAM role + instance profile ---------------------------------------------
role="${P}-app"
aws iam create-role --role-name "$role" --tags "$(tag_json "$F" "$RUN" "$role")" \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
ssm_core="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
aws iam attach-role-policy --role-name "$role" --policy-arn "$ssm_core"
aws iam create-instance-profile --instance-profile-name "$role" --tags "$(tag_json "$F" "$RUN" "$role")" >/dev/null
aws iam add-role-to-instance-profile --instance-profile-name "$role" --role-name "$role"
aws iam wait instance-profile-exists --instance-profile-name "$role"
manifest_add aws_iam_role "$role" adopt "$created"
manifest_add aws_iam_role_policy_attachment "${role}/${ssm_core}" adopt "$created"
manifest_add aws_iam_instance_profile "$role" adopt "$created"
manifest_add aws_iam_policy "$ssm_core" exclude "AWS-managed policy: referenced, never imported"

# --- EC2 instance + extra EBS volume -----------------------------------------
ami="$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)"
# A new instance profile takes a few seconds to become usable by EC2.
instance="$(retry aws ec2 run-instances --image-id "$ami" --instance-type t3.micro --subnet-id "$priv_a" \
  --security-group-ids "$app_sg" --iam-instance-profile Name="$role" \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled \
  --tag-specifications "$(tags instance "$F" "$RUN" f2-app)" "$(tags volume "$F" "$RUN" f2-app-root)" \
  --query 'Instances[0].InstanceId' --output text)"
aws ec2 wait instance-running --instance-ids "$instance"
manifest_add aws_instance "$instance" adopt "$created"
root_vol="$(aws ec2 describe-instances --instance-ids "$instance" \
  --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text)"
eni="$(aws ec2 describe-instances --instance-ids "$instance" \
  --query 'Reservations[0].Instances[0].NetworkInterfaces[0].NetworkInterfaceId' --output text)"
manifest_add aws_ebs_volume "$root_vol" exclude "root volume, managed through aws_instance root_block_device"
manifest_add aws_network_interface "$eni" exclude "primary ENI, managed through aws_instance"

data_vol="$(aws ec2 create-volume --availability-zone "$AZ_A" --size 8 --volume-type gp3 --encrypted \
  --tag-specifications "$(tags volume "$F" "$RUN" f2-app-data)" --query VolumeId --output text)"
aws ec2 wait volume-available --volume-ids "$data_vol"
aws ec2 attach-volume --volume-id "$data_vol" --instance-id "$instance" --device /dev/sdf >/dev/null
aws ec2 wait volume-in-use --volume-ids "$data_vol"
manifest_add aws_ebs_volume "$data_vol" adopt "$created"
manifest_add aws_volume_attachment "/dev/sdf:${data_vol}:${instance}" adopt "$created"

# --- S3 bucket with split sub-resources --------------------------------------
bucket="${P}-${SANDBOX_ACCOUNT_ID}"
create_bucket "$bucket" "$F" "$RUN"
aws s3api put-bucket-versioning --bucket "$bucket" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$bucket" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"},"BucketKeyEnabled":true}]}'
aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-ownership-controls --bucket "$bucket" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
aws s3api put-bucket-policy --bucket "$bucket" --policy "$(jq -nc --arg b "arn:aws:s3:::$bucket" \
  '{Version:"2012-10-17",Statement:[{Sid:"DenyInsecureTransport",Effect:"Deny",Principal:"*",Action:"s3:*",
    Resource:[$b,($b+"/*")],Condition:{Bool:{"aws:SecureTransport":"false"}}}]}')"
aws s3api put-bucket-lifecycle-configuration --bucket "$bucket" --lifecycle-configuration \
  '{"Rules":[{"ID":"tidy-versions","Status":"Enabled","Filter":{},"NoncurrentVersionExpiration":{"NoncurrentDays":30},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]}'
for t in aws_s3_bucket aws_s3_bucket_versioning aws_s3_bucket_server_side_encryption_configuration \
  aws_s3_bucket_public_access_block aws_s3_bucket_ownership_controls aws_s3_bucket_policy \
  aws_s3_bucket_lifecycle_configuration; do
  manifest_add "$t" "$bucket" adopt "$created"
done

echo "F2 created (run $RUN). Manifest: $MANIFEST"

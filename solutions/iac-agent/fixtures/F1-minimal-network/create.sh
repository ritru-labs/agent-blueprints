#!/usr/bin/env bash
# F1 minimal: 1 VPC, 2 subnets, an internet gateway and a public route table.
# Expected: every created resource adopted at 0 changes; the VPC's
# AWS-created defaults (main route table, default SG, default NACL) excluded.
set -euo pipefail
# shellcheck source=fixtures/lib.sh
source "$(dirname "$0")/../lib.sh"

guard_sandbox
F=F1 RUN="$(new_run_id)"
manifest_init "$F" "$RUN"
read -r AZ_A AZ_B <<<"$(first_two_azs)"

vpc="$(aws ec2 create-vpc --cidr-block 10.42.0.0/16 \
  --tag-specifications "$(tags vpc "$F" "$RUN" f1-vpc)" \
  --query Vpc.VpcId --output text)"
aws ec2 wait vpc-available --vpc-ids "$vpc"
aws ec2 modify-vpc-attribute --vpc-id "$vpc" --enable-dns-hostnames '{"Value":true}'
manifest_add aws_vpc "$vpc" adopt "created by fixture"

sub_a="$(aws ec2 create-subnet --vpc-id "$vpc" --cidr-block 10.42.1.0/24 --availability-zone "$AZ_A" \
  --tag-specifications "$(tags subnet "$F" "$RUN" f1-public-a)" --query Subnet.SubnetId --output text)"
sub_b="$(aws ec2 create-subnet --vpc-id "$vpc" --cidr-block 10.42.2.0/24 --availability-zone "$AZ_B" \
  --tag-specifications "$(tags subnet "$F" "$RUN" f1-public-b)" --query Subnet.SubnetId --output text)"
aws ec2 modify-subnet-attribute --subnet-id "$sub_a" --map-public-ip-on-launch
manifest_add aws_subnet "$sub_a" adopt "created by fixture"
manifest_add aws_subnet "$sub_b" adopt "created by fixture"

igw="$(aws ec2 create-internet-gateway \
  --tag-specifications "$(tags internet-gateway "$F" "$RUN" f1-igw)" \
  --query InternetGateway.InternetGatewayId --output text)"
aws ec2 attach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$vpc"
manifest_add aws_internet_gateway "$igw" adopt "created by fixture"

rtb="$(aws ec2 create-route-table --vpc-id "$vpc" \
  --tag-specifications "$(tags route-table "$F" "$RUN" f1-public-rt)" \
  --query RouteTable.RouteTableId --output text)"
aws ec2 create-route --route-table-id "$rtb" --destination-cidr-block 0.0.0.0/0 --gateway-id "$igw" >/dev/null
manifest_add aws_route_table "$rtb" adopt "created by fixture"
manifest_add aws_route "${rtb}_0.0.0.0/0" adopt "created by fixture"

for s in "$sub_a" "$sub_b"; do
  aws ec2 associate-route-table --route-table-id "$rtb" --subnet-id "$s" >/dev/null
  manifest_add aws_route_table_association "${s}/${rtb}" adopt "created by fixture"
done

manifest_vpc_defaults "$vpc"

echo "F1 created (run $RUN). Manifest: $MANIFEST"

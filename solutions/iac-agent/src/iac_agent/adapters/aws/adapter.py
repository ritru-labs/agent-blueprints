"""The AWS adapter as the pipeline sees it (core.graph.Adapter)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

import boto3

from ...core.models import Classification, Discovery
from . import import_ids
from .best_effort import list_best_effort
from .discovery import boto3_clients, discover
from .ownership import classify, state_keys


class AwsAdapter:
    inline_blocks = import_ids.INLINE_BLOCKS
    identity_attrs = import_ids.IDENTITY_ATTRS
    secret_attrs = {"aws_instance": {"user_data", "user_data_base64"}}

    def __init__(self, session: boto3.Session, region: str, state_files: Iterable[Path] = (),
                 best_effort_types: Iterable[str] = ()):  # fmt: skip
        self.session, self.region = session, region
        self.in_state = set().union(*(state_keys(json.loads(Path(p).read_text())) for p in state_files))
        self.best_effort_types = list(best_effort_types)
        self._account: str | None = None

    def identity(self) -> tuple[str, str]:
        sts = self.session.client("sts", region_name=self.region)
        self._account = sts.get_caller_identity()["Account"]
        return self._account, self.region

    def discover(self) -> Discovery:
        clients = boto3_clients(self.session, self.region)
        result = discover(clients, self._account or self.identity()[0], self.region)
        if self.best_effort_types:
            resources, coverage = list_best_effort(clients, self.best_effort_types)
            result.resources += resources
            result.coverage += coverage
        return result

    def classify(self, discovery: Discovery) -> list[Classification]:
        return classify(discovery, self.in_state)

    FILES = {
        "aws_vpc": "network.tf", "aws_subnet": "network.tf", "aws_internet_gateway": "network.tf",
        "aws_nat_gateway": "network.tf", "aws_eip": "network.tf", "aws_route_table": "network.tf",
        "aws_route": "network.tf", "aws_route_table_association": "network.tf",
        "aws_security_group": "security_groups.tf", "aws_vpc_security_group_ingress_rule": "security_groups.tf",
        "aws_vpc_security_group_egress_rule": "security_groups.tf",
        "aws_instance": "ec2.tf", "aws_ebs_volume": "ec2.tf", "aws_volume_attachment": "ec2.tf",
        "aws_key_pair": "ec2.tf",
    }  # fmt: skip

    def file_for(self, terraform_type: str) -> str:
        if terraform_type.startswith("aws_s3_"):
            return "s3.tf"
        if terraform_type.startswith("aws_iam_"):
            return "iam.tf"
        return self.FILES.get(terraform_type, "other.tf")

    def provider_files(self, versions: Mapping[str, str], account: str) -> dict[str, str]:
        return {
            "versions.tf": (
                "terraform {\n"
                f'  required_version = "= {versions["terraform"]}"\n'
                "  required_providers {\n"
                "    aws = {\n"
                '      source  = "hashicorp/aws"\n'
                f'      version = "= {versions["terraform_provider_aws"]}"\n'
                "    }\n  }\n}\n"
            ),
            # allowed_account_ids: the provider itself refuses any other account.
            "providers.tf": (
                f'provider "aws" {{\n  region              = "{self.region}"\n'
                f'  allowed_account_ids = ["{account}"]\n}}\n'
            ),
        }

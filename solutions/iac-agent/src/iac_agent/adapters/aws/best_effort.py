"""Best-effort tier: list other resource types through the Cloud Control API.

Opt-in per run (`types`), empty by default. Only types whose Cloud Control
identifier equals the Terraform import ID are mapped here, so no ID format is
guessed. Resource properties are dropped: they can hold secrets, and the plan
gate decides adoption anyway (0 changes or skipped).

Enabling a type needs `cloudcontrol:ListResources` plus that service's list
permission added to iam/scanner-policy.json (and its tests). V1 adds none.
"""

from __future__ import annotations

from collections.abc import Iterable

from botocore.exceptions import BotoCoreError, ClientError

from ...core.models import Coverage, Resource
from .discovery import ClientFactory, _error

# Cloud Control type -> Terraform type, where identifier == import ID (provider docs, "Import").
TYPES = {
    "AWS::Logs::LogGroup": "aws_cloudwatch_log_group",  # name
    "AWS::SNS::Topic": "aws_sns_topic",  # ARN
    "AWS::SQS::Queue": "aws_sqs_queue",  # queue URL
    "AWS::DynamoDB::Table": "aws_dynamodb_table",  # name
    "AWS::ECR::Repository": "aws_ecr_repository",  # name
}


def list_best_effort(clients: ClientFactory, types: Iterable[str]) -> tuple[list[Resource], list[Coverage]]:
    cc = clients("cloudcontrol")
    resources: list[Resource] = []
    coverage: list[Coverage] = []
    for type_name in types:
        terraform_type = TYPES.get(type_name)
        if terraform_type is None:
            coverage.append(Coverage(terraform_type=type_name, complete=False, error="no fixed import ID mapping"))
            continue
        try:
            found = [
                Resource(
                    terraform_type=terraform_type,
                    import_id=d["Identifier"],
                    attributes={"best_effort": True, "cloudcontrol_type": type_name},
                )
                for page in cc.get_paginator("list_resources").paginate(TypeName=type_name)
                for d in page.get("ResourceDescriptions", [])
            ]
        except (ClientError, BotoCoreError) as e:
            coverage.append(Coverage(terraform_type=terraform_type, complete=False, error=_error(e)))
            continue
        resources += found
        coverage.append(Coverage(terraform_type=terraform_type, complete=True, count=len(found)))
    return resources, coverage

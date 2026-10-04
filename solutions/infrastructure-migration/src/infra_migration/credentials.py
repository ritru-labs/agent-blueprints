"""Short-lived AWS read leases. Does not create or modify IAM roles."""

import json
import re

from .models import Principal, Scope
from .tools import AccessDenied

READ_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": ["ec2:Describe*", "sts:GetCallerIdentity"], "Resource": "*"}
    ],
}


class ReadCredentialBroker:
    def __init__(self, sts_client, role_arn: str, passphrase_reader):
        self.sts = sts_client
        self.role_arn, self.passphrase_reader = role_arn, passphrase_reader

    def lease(self, principal: Principal, scope: Scope):
        if principal.tenant_id != scope.tenant_id or "executor" not in principal.roles:
            raise AccessDenied("Credential lease authorization failed")
        if not re.fullmatch(
            f"arn:aws:iam::{scope.account_id}:role/[A-Za-z0-9_+=,.@/-]+", self.role_arn
        ):
            raise AccessDenied("Credential role is outside the cloud account")
        result = self.sts.assume_role(
            RoleArn=self.role_arn,
            RoleSessionName="infra-migration-read",
            DurationSeconds=900,
            Policy=json.dumps(READ_POLICY),
        )
        credentials = result["Credentials"]
        return {
            "AWS_ACCESS_KEY_ID": credentials["AccessKeyId"],
            "AWS_SECRET_ACCESS_KEY": credentials["SecretAccessKey"],
            "AWS_SESSION_TOKEN": credentials["SessionToken"],
            "AWS_REGION": scope.regions[0],
            "PULUMI_CONFIG_PASSPHRASE": self.passphrase_reader(),
        }

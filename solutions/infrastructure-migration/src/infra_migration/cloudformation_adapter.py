"""Reviewed CloudFormation change-set adapter with exact retention/release proposals."""

import json
import re
import time

from .generation import inventory_fingerprint
from .models import digest
from .source import parse_template, release_proposal, retention_proposal
from .tools import AccessDenied


class CloudFormationTransferAdapter:
    version = "cloudformation-vpc-subnet-transfer-v1"

    def __init__(
        self,
        client,
        stack_id: str,
        logical_ids: tuple[str, ...],
        proposed_template: dict,
        observe_inventory,
        freeze_verified,
        health_verified,
        *,
        timeout=60,
    ):
        self.client, self.stack_id, self.logical_ids = client, stack_id, logical_ids
        self.proposed_template = json.loads(json.dumps(proposed_template))
        self.observe_inventory = observe_inventory
        self.freeze_verified, self.health_verified, self.timeout = (
            freeze_verified,
            health_verified,
            timeout,
        )

    def _source(self):
        template = self.client.get_template(StackName=self.stack_id, TemplateStage="Original")[
            "TemplateBody"
        ]
        if isinstance(template, str):
            template = parse_template(template)
        stack = self.client.describe_stacks(StackName=self.stack_id)["Stacks"][0]
        resources = self.client.describe_stack_resources(StackName=self.stack_id)["StackResources"]
        return template, stack, resources

    def observe(self, binding):
        if binding.action not in {"source_retain", "source_release"}:
            raise AccessDenied("CloudFormation action mismatch")
        match = re.fullmatch(
            r"arn:aws:cloudformation:([^:]+):([0-9]{12}):stack/[^/]+/[^/]+", self.stack_id
        )
        if (
            not match
            or match.group(1) != binding.scope.regions[0]
            or match.group(2) != binding.scope.account_id
        ):
            raise AccessDenied("CloudFormation source ARN is outside scope")
        template, stack, resources = self._source()
        if stack["StackId"] != self.stack_id or binding.scope.account_id not in self.stack_id:
            raise AccessDenied("CloudFormation source scope mismatch")
        if f":{binding.scope.regions[0]}:" not in self.stack_id:
            raise AccessDenied("CloudFormation region mismatch")
        if stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"}:
            raise AccessDenied("CloudFormation source is not stable")
        proposal = (
            retention_proposal(template, self.logical_ids)
            if binding.action == "source_retain"
            else release_proposal(template, self.logical_ids)
        )
        if proposal != self.proposed_template or digest(proposal) != binding.artifact_digest:
            raise AccessDenied("Source proposal contains unexpected changes")
        selected = [r for r in resources if r["LogicalResourceId"] in self.logical_ids]
        if {r["PhysicalResourceId"] for r in selected} != set(binding.resources):
            raise AccessDenied("Source logical/physical resource mapping differs")
        baseline = {
            "template": template,
            "resources": sorted(resources, key=lambda r: r["LogicalResourceId"]),
        }
        # Remove incidental observation timestamps from the source-state fingerprint.
        for resource in baseline["resources"]:
            resource.pop("Timestamp", None)
        plan = {
            "source_stack": self.stack_id,
            "action": binding.action,
            "template_digest": digest(proposal),
        }
        inventory = self.observe_inventory()
        return {
            "artifact_digest": digest(proposal),
            "inventory_digest": inventory_fingerprint(inventory),
            "state_digest": digest(baseline),
            "plan_digest": digest(plan),
            "blockers": [] if self.freeze_verified(binding) is True else ["SOURCE_NOT_FROZEN"],
        }

    def execute(self, binding, operation):
        # Coordinator performs two bound observations, including one while holding resource locks.
        _, stack, _ = self._source()
        result = self.client.create_change_set(
            StackName=self.stack_id,
            ChangeSetName="migration-" + operation,
            ChangeSetType="UPDATE",
            TemplateBody=json.dumps(self.proposed_template),
            Parameters=[
                {"ParameterKey": p["ParameterKey"], "UsePreviousValue": True}
                for p in stack.get("Parameters", [])
            ],
            Capabilities=["CAPABILITY_NAMED_IAM"],
            ClientToken=operation,
        )
        change_id = result["Id"]
        deadline = time.monotonic() + self.timeout
        while True:
            change = self.client.describe_change_set(ChangeSetName=change_id)
            if change["Status"] == "CREATE_COMPLETE":
                break
            if change["Status"] == "FAILED" or time.monotonic() >= deadline:
                raise AccessDenied(
                    "Change-set preparation failed or timed out; reconcile before retry"
                )
            time.sleep(1)
        allowed = "Modify" if binding.action == "source_retain" else "Remove"
        if change.get("NextToken"):
            raise AccessDenied("Paginated change-set review is not qualified")
        for entry in change.get("Changes", []):
            resource = entry.get("ResourceChange", {})
            if (
                resource.get("LogicalResourceId") not in self.logical_ids
                or resource.get("Action") != allowed
                or resource.get("Replacement", "False") != "False"
            ):
                raise AccessDenied("Change set contains unexpected resource effects")
            if binding.action == "source_retain" and (
                not resource.get("Scope")
                or set(resource["Scope"]) - {"DeletionPolicy", "UpdateReplacePolicy"}
            ):
                raise AccessDenied("Retention change set modifies more than ownership policies")
        self.client.execute_change_set(ChangeSetName=change_id, ClientRequestToken=operation)
        while time.monotonic() < deadline:
            outcome, receipt = self.reconcile(binding, operation)
            if outcome == "SUCCEEDED":
                return receipt
            time.sleep(1)
        raise AccessDenied("Source change did not verify within the execution budget")

    def reconcile(self, binding, operation):
        template, stack, resources = self._source()
        if template != self.proposed_template or stack["StackStatus"] != "UPDATE_COMPLETE":
            return "OUTCOME_UNKNOWN", {}
        selected = {r["LogicalResourceId"] for r in resources} & set(self.logical_ids)
        if binding.action == "source_release" and selected:
            return "OUTCOME_UNKNOWN", {}
        inventory = self.observe_inventory()
        if inventory_fingerprint(inventory) != binding.inventory_digest:
            return "OUTCOME_UNKNOWN", {}
        if self.freeze_verified(binding) is not True or self.health_verified(binding) is not True:
            return "OUTCOME_UNKNOWN", {}
        return "SUCCEEDED", {
            "binding_digest": digest(binding),
            "operation": operation,
            "verified": True,
            "source_template_digest": digest(template),
        }

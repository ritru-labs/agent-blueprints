"""Pulumi state adoption adapter. Enabled only through trusted qualification admission."""

import json
from pathlib import Path

from .generation import (
    ProjectBundle,
    generate,
    inventory_fingerprint,
    symbol,
    verify_bundle_directory,
)
from .ledger import ExecutionBinding
from .models import digest
from .tools import AccessDenied


def validate_preview(payload: dict):
    steps = payload.get("steps")
    if not isinstance(steps, list):
        raise AccessDenied("Preview does not contain structured steps")
    for step in steps:
        if step.get("op") not in {
            "same",
            "read",
            "read-replacement",
            "import",
            "import-replacement",
        }:
            raise AccessDenied("Preview contains a non-adoption change")
        if step.get("op") in {"read-replacement", "import-replacement"}:
            raise AccessDenied("Replacement is forbidden during adoption")
    return True


def verify_state(binding: ExecutionBinding, bundle: ProjectBundle, state: dict):
    manifest = json.loads(bundle.files["import.json"])["resources"]
    entries = state.get("deployment", {}).get("resources", [])
    if not isinstance(entries, list):
        raise AccessDenied("State resources are invalid")
    expected = {m["name"]: m for m in manifest}
    expected_inputs = json.loads(bundle.files["expected-inputs.json"])
    providers = {r.get("urn"): r for r in entries if r.get("type") == "pulumi:providers:aws"}
    found = {}
    for resource in entries:
        urn = resource.get("urn", "")
        name = urn.rsplit("::", 1)[-1]
        if name in expected:
            if name in found:
                raise AccessDenied("Duplicate managed resource identity")
            found[name] = resource
    if set(found) != set(expected):
        raise AccessDenied("State is missing imported resources")
    for name, item in found.items():
        if (
            item.get("id") != expected[name]["id"]
            or item.get("type") != expected[name]["type"]
            or item.get("protect") is not True
            or item.get("delete") is True
        ):
            raise AccessDenied("Imported identity, type or protection differs")
        stack_name = binding.destination.rsplit("/", 1)[-1]
        expected_urn = f"urn:pulumi:{stack_name}::infra-migration-generated::{item['type']}::{name}"
        if item.get("urn") != expected_urn:
            raise AccessDenied("Imported resource belongs to a different stack or project")
        if any(
            item.get("inputs", {}).get(key) != value for key, value in expected_inputs[name].items()
        ):
            raise AccessDenied("Imported configuration differs from the observed baseline")
        provider_urn = item.get("provider", "").rsplit("::", 1)[0]
        provider = providers.get(provider_urn, {}).get("inputs", {})
        accounts = provider.get("allowedAccountIds")
        if isinstance(accounts, str):
            accounts = json.loads(accounts)
        if provider.get("region") != binding.scope.regions[0] or accounts != [
            binding.scope.account_id
        ]:
            raise AccessDenied("Imported provider is not bound to the account and region")
    if set(bundle.resource_ids) != set(binding.resources):
        raise AccessDenied("Binding resource list differs from manifest")


class PulumiImportAdapter:
    version = "pulumi-aws-vpc-subnet-v1"

    def __init__(
        self,
        runner,
        bundle: ProjectBundle,
        directory: Path,
        state_directory: Path,
        credential_lease,
        observe_inventory,
        destination: str,
        verify_health,
    ):
        self.runner, self.bundle, self.directory = runner, bundle, directory
        self.state_directory, self.credential_lease = state_directory, credential_lease
        self.observe_inventory, self.destination = observe_inventory, destination
        self.verify_health = verify_health

    def _run(self, args):
        return self.runner.run(
            self.bundle,
            self.directory,
            args,
            state_directory=self.state_directory,
            credentials=self.credential_lease(),
        )

    def _state(self):
        return json.loads(self._run(["pulumi", "stack", "export", "--stack", self.destination]))

    def _check(self, binding):
        if binding.action != "import" or binding.destination != self.destination:
            raise AccessDenied("Pulumi adapter action or destination mismatch")
        if binding.artifact_digest != self.bundle.artifact_digest:
            raise AccessDenied("Pulumi artifact mismatch")
        if binding.inventory_digest != self.bundle.inventory_digest:
            raise AccessDenied("Pulumi inventory binding mismatch")
        verify_bundle_directory(self.bundle, self.directory)

    def observe(self, binding):
        self._check(binding)
        inventory = self.observe_inventory()
        canonical = generate(inventory, self.bundle.resource_ids)
        if canonical.artifact_digest != self.bundle.artifact_digest:
            raise AccessDenied(
                "Program differs from canonical generation of observed infrastructure"
            )
        state = self._state()
        # Block already managed identities and unknown ownership before importing.
        blockers = list(self.bundle.blockers)
        if set(binding.resources) != set(self.bundle.resource_ids):
            blockers.append("RESOURCE_SELECTION_MISMATCH")
        imported_ids = {r.get("id") for r in state.get("deployment", {}).get("resources", [])}
        if imported_ids & set(binding.resources):
            blockers.append("RESOURCE_ALREADY_IN_DESTINATION_STATE")
        plan = {
            "action": "import",
            "manifest": json.loads(self.bundle.files["import.json"]),
            "artifact_digest": self.bundle.artifact_digest,
            "destination": self.destination,
        }
        return {
            "inventory_digest": inventory_fingerprint(inventory),
            "state_digest": digest(state),
            "artifact_digest": self.bundle.artifact_digest,
            "plan_digest": digest(plan),
            "blockers": blockers,
        }

    def execute(self, binding, operation):
        self._check(binding)
        self._run(
            [
                "pulumi",
                "import",
                "--file",
                "/project/import.json",
                "--stack",
                self.destination,
                "--config-file",
                "/project/stack-config.json",
                "--yes",
                "--non-interactive",
                "--protect=true",
                "--generate-code=false",
            ]
        )
        state = self._state()
        verify_state(binding, self.bundle, state)
        preview = json.loads(
            self._run(
                [
                    "pulumi",
                    "preview",
                    "--stack",
                    self.destination,
                    "--config-file",
                    "/project/stack-config.json",
                    "--json",
                    "--non-interactive",
                    "--expect-no-changes",
                ]
            )
        )
        validate_preview(preview)
        if inventory_fingerprint(self.observe_inventory()) != binding.inventory_digest:
            raise AccessDenied("Cloud configuration changed during adoption")
        if self.verify_health(binding) is not True:
            raise AccessDenied("Application health verification failed")
        return {
            "binding_digest": digest(binding),
            "operation": operation,
            "verified": True,
            "state_digest": digest(state),
            "preview_digest": digest(preview),
        }

    def reconcile(self, binding, operation):
        self._check(binding)
        state = self._state()
        entries = state.get("deployment", {}).get("resources", [])
        names = {r.get("urn", "").rsplit("::", 1)[-1] for r in entries}
        selected_names = {symbol(r) for r in binding.resources}
        if not names & selected_names:
            # Destination absence cannot alone prove source ownership or cloud health.
            return "OUTCOME_UNKNOWN", {}
        try:
            verify_state(binding, self.bundle, state)
            preview = json.loads(
                self._run(
                    [
                        "pulumi",
                        "preview",
                        "--stack",
                        self.destination,
                        "--config-file",
                        "/project/stack-config.json",
                        "--json",
                        "--non-interactive",
                        "--expect-no-changes",
                    ]
                )
            )
            validate_preview(preview)
            if (
                inventory_fingerprint(self.observe_inventory()) != binding.inventory_digest
                or self.verify_health(binding) is not True
            ):
                return "OUTCOME_UNKNOWN", {}
        except AccessDenied:
            return "OUTCOME_UNKNOWN", {}
        return "SUCCEEDED", {
            "binding_digest": digest(binding),
            "operation": operation,
            "verified": True,
            "state_digest": digest(state),
        }

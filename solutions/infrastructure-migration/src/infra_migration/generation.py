"""Deterministic TypeScript generation from typed observed configurations."""

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .models import Contract, Inventory, digest
from .tools import AccessDenied

AWS_VERSION = "7.48.0"
PULUMI_VERSION = "3.267.0"
TS_VERSION = "5.9.3"


def inventory_fingerprint(inventory: Inventory):
    data = inventory.model_dump(mode="json")
    data.pop("observed_at")
    data["resources"] = sorted(data["resources"], key=lambda r: r["resource_id"])
    data["gaps"] = sorted(data["gaps"])
    return digest(data)


class VpcConfiguration(Contract):
    cidrBlock: str
    instanceTenancy: Literal["default", "dedicated"]
    enableDnsSupport: bool
    enableDnsHostnames: bool
    enableNetworkAddressUsageMetrics: bool
    tags: dict[str, str]


class SubnetConfiguration(Contract):
    vpcId: str
    cidrBlock: str
    availabilityZoneId: str
    mapPublicIpOnLaunch: bool
    privateDnsHostnameTypeOnLaunch: Literal["ip-name", "resource-name"]
    enableResourceNameDnsARecordOnLaunch: bool
    enableResourceNameDnsAaaaRecordOnLaunch: bool
    tags: dict[str, str]


class ProjectBundle(Contract):
    inventory_digest: str
    files: dict[str, str]
    resource_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def check_digest(self):
        if self.artifact_digest != digest(self.files):
            raise ValueError("Bundle digest mismatch")
        if any(Path(p).name != p for p in self.files):
            raise ValueError("Bundle paths must be simple filenames")
        manifest = json.loads(self.files["import.json"])["resources"]
        identities = [item["id"] for item in manifest]
        if len(set(identities)) != len(identities) or set(identities) != set(self.resource_ids):
            raise ValueError("Bundle resource identities do not match its import manifest")
        if any(
            item["name"] != symbol(item["id"]) or item["version"] != AWS_VERSION
            for item in manifest
        ):
            raise ValueError("Manifest naming or provider version differs from generator policy")
        return self


def symbol(identity: str):
    return "r_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def generate(inventory: Inventory, selected_ids: tuple[str, ...]) -> ProjectBundle:
    if not selected_ids or len(set(selected_ids)) != len(selected_ids):
        raise AccessDenied("Select unique resources explicitly")
    selected = [r for r in inventory.resources if r.resource_id in selected_ids]
    if len(selected) != len(selected_ids):
        raise AccessDenied("Selection contains undiscovered resources")
    if len({r.region for r in selected}) != 1:
        raise AccessDenied("Each generated batch must use one region")
    region = selected[0].region
    types = {r.resource_id: r.resource_type for r in selected}
    selected.sort(key=lambda r: (r.resource_type != "AWS::EC2::VPC", r.resource_id))
    statements = ['import * as aws from "@pulumi/aws";']
    manifest, expected_inputs, blockers = [], {}, list(inventory.gaps)
    for resource in selected:
        if resource.blockers:
            raise AccessDenied(f"Resource requires another adapter: {resource.resource_id}")
        if resource.resource_type == "AWS::EC2::VPC":
            args = VpcConfiguration.model_validate(resource.configuration).model_dump()
            constructor, token = "Vpc", "aws:ec2/vpc:Vpc"
        elif resource.resource_type == "AWS::EC2::Subnet":
            args = SubnetConfiguration.model_validate(resource.configuration).model_dump()
            if args["vpcId"] not in selected_ids:
                raise AccessDenied("Subnet VPC must be included in the batch")
            if types[args["vpcId"]] != "AWS::EC2::VPC" or resource.dependencies != (args["vpcId"],):
                raise AccessDenied("Subnet dependency does not match the observed VPC")
            constructor, token = "Subnet", "aws:ec2/subnet:Subnet"
        else:
            raise AccessDenied("Resource type has no configuration generator")
        name = symbol(resource.resource_id)
        expected_inputs[name] = dict(args)
        tags = args.pop("tags")
        tags_expression = (
            "JSON.parse(" + json.dumps(json.dumps(tags, sort_keys=True, ensure_ascii=True)) + ")"
        )
        if constructor == "Subnet":
            vpc = args.pop("vpcId")
            encoded = json.dumps(args, sort_keys=True, ensure_ascii=True)[:-1]
            encoded += ', "vpcId": ' + symbol(vpc) + '.id, "tags": ' + tags_expression + "}"
        else:
            encoded = json.dumps(args, sort_keys=True, ensure_ascii=True)[:-1]
            encoded += ', "tags": ' + tags_expression + "}"
        statements.append(
            f"const {name} = new aws.ec2.{constructor}({json.dumps(name)}, "
            f"{encoded}, {{protect: true}});"
        )
        statements.append(f"export const {name}_id = {name}.id;")
        manifest.append(
            {"type": token, "name": name, "id": resource.resource_id, "version": AWS_VERSION}
        )
        if resource.owner == "unknown":
            blockers.append(f"OWNERSHIP_UNKNOWN:{resource.resource_id}")
        elif resource.owner == "cloudformation":
            blockers.append(f"SOURCE_OWNERSHIP_NOT_RELEASED:{resource.resource_id}")
    files = {
        "index.ts": "\n".join(statements) + "\n",
        "package.json": json.dumps(
            {
                "name": "infra-migration-generated",
                "version": "1.0.0",
                "private": True,
                "dependencies": {"@pulumi/aws": AWS_VERSION, "@pulumi/pulumi": PULUMI_VERSION},
                "devDependencies": {"typescript": TS_VERSION},
            },
            indent=2,
        )
        + "\n",
        "tsconfig.json": json.dumps(
            {
                "compilerOptions": {
                    "target": "ES2020",
                    "module": "commonjs",
                    "strict": True,
                    "skipLibCheck": True,
                    "noEmit": True,
                },
                "files": ["index.ts"],
            }
        ),
        "Pulumi.yaml": "name: infra-migration-generated\nruntime: nodejs\n",
        "stack-config.json": json.dumps(
            {
                "config": {
                    "aws:region": region,
                    "aws:allowedAccountIds": [inventory.scope.account_id],
                }
            }
        ),
        "import.json": json.dumps({"resources": manifest}, indent=2) + "\n",
        "expected-inputs.json": json.dumps(expected_inputs, sort_keys=True, ensure_ascii=True),
    }
    return ProjectBundle(
        inventory_digest=inventory_fingerprint(inventory),
        files=files,
        resource_ids=tuple(sorted(selected_ids)),
        blockers=tuple(sorted(set(blockers))),
        artifact_digest=digest(files),
    )


def write_bundle(bundle: ProjectBundle, destination: Path):
    # Create a new directory rather than overwrite or follow existing destination symlinks.
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in bundle.files.items():
        (destination / name).write_text(content)
    (destination / "bundle.json").write_text(bundle.model_dump_json(indent=2) + "\n")


def verify_bundle_directory(bundle: ProjectBundle, directory: Path):
    if digest(bundle.files) != bundle.artifact_digest:
        raise AccessDenied("In-memory bundle content changed after verification")
    if directory.is_symlink() or not directory.is_dir():
        raise AccessDenied("Project directory must not be a symlink")
    if {p.name for p in directory.iterdir()} != set(bundle.files) | {"bundle.json"}:
        raise AccessDenied("Project contains unexpected files")
    manifest = directory / "bundle.json"
    if manifest.is_symlink() or ProjectBundle.model_validate_json(manifest.read_text()) != bundle:
        raise AccessDenied("Bundle manifest changed")
    for name, content in bundle.files.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.read_text() != content:
            raise AccessDenied("Generated artifact changed or is not a regular file")

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import boto3
import httpx
import pytest
from botocore.stub import Stubber
from langgraph.checkpoint.sqlite import SqliteSaver

from infra_migration.credentials import ReadCredentialBroker
from infra_migration.demo import fixture
from infra_migration.discovery import AwsDiscovery
from infra_migration.execution import Executor
from infra_migration.generation import (
    ProjectBundle,
    generate,
    inventory_fingerprint,
    verify_bundle_directory,
    write_bundle,
)
from infra_migration.ledger import ExecutionBinding, LocalLedger
from infra_migration.models import Inventory, Principal, ReviewDecision, digest
from infra_migration.pipeline import MigrationPipeline
from infra_migration.pulumi_adapter import validate_preview, verify_state
from infra_migration.reasoning import ModelReviewer, fetch_document
from infra_migration.runner import DockerRunner
from infra_migration.source import parse_template, release_proposal, retention_proposal
from infra_migration.tools import AccessDenied


def configured_inventory():
    data = fixture().model_dump(mode="json")
    data["coverage"], data["gaps"] = "complete_fixture", []
    data["resources"] = data["resources"][:2]
    data["resources"][0]["configuration"] = {
        "cidrBlock": "10.0.0.0/16",
        "instanceTenancy": "default",
        "enableDnsSupport": True,
        "enableDnsHostnames": True,
        "enableNetworkAddressUsageMetrics": False,
        "tags": {"Name": "network", "__proto__": "preserve", "note": '";process.exit(1);//'},
    }
    data["resources"][1]["configuration"] = {
        "vpcId": "vpc-fixture",
        "cidrBlock": "10.0.1.0/24",
        "availabilityZoneId": "use1-az1",
        "mapPublicIpOnLaunch": False,
        "privateDnsHostnameTypeOnLaunch": "ip-name",
        "enableResourceNameDnsARecordOnLaunch": False,
        "enableResourceNameDnsAaaaRecordOnLaunch": False,
        "tags": {"source": "vpc-fixture"},
    }
    return Inventory.model_validate_json(json.dumps(data))


def actors(inventory):
    return [
        Principal(tenant_id=inventory.scope.tenant_id, subject=subject, roles=(role,))
        for subject, role in (
            ("requester", "assessor"),
            ("reviewer", "reviewer"),
            ("worker", "executor"),
        )
    ]


def bundle_and_binding():
    inventory = configured_inventory()
    bundle = generate(inventory, ("vpc-fixture", "subnet-fixture"))
    binding = ExecutionBinding(
        scope=inventory.scope,
        action="import",
        requester="requester",
        artifact_digest=bundle.artifact_digest,
        inventory_digest=inventory_fingerprint(inventory),
        destination_state_digest="1" * 64,
        plan_digest="2" * 64,
        destination="dev",
        resources=bundle.resource_ids,
        adapter_version="test-v1",
    )
    return inventory, bundle, binding


class Adapter:
    version = "test-v1"

    def __init__(self, binding, fail=False):
        self.binding, self.fail, self.writes = binding, fail, 0

    def observe(self, binding):
        return {
            "inventory_digest": binding.inventory_digest,
            "state_digest": binding.destination_state_digest,
            "artifact_digest": binding.artifact_digest,
            "plan_digest": binding.plan_digest,
            "blockers": [],
        }

    def execute(self, binding, operation):
        self.writes += 1
        if self.fail:
            raise TimeoutError("Synthetic unknown outcome")
        return {"binding_digest": digest(binding), "verified": True}

    def reconcile(self, binding, operation):
        return "SUCCEEDED", {"binding_digest": digest(binding), "verified": True}


def test_generation_uses_observed_configuration_and_safe_tag_encoding(tmp_path):
    inventory, bundle, _ = bundle_and_binding()
    write_bundle(bundle, tmp_path / "project")
    verify_bundle_directory(bundle, tmp_path / "project")
    inputs = json.loads(bundle.files["expected-inputs.json"])
    assert any(i["tags"].get("__proto__") == "preserve" for i in inputs.values())
    assert '"source": "vpc-fixture"' in json.dumps(inputs)
    assert "JSON.parse(" in bundle.files["index.ts"]
    assert "protect: true" in bundle.files["index.ts"]
    assert "ignoreChanges" not in bundle.files["index.ts"]
    assert bundle.inventory_digest == inventory_fingerprint(inventory)


@pytest.mark.parametrize("change", ["extra_file", "edited_code", "symlink"])
def test_artifact_intake_rejects_changes_and_extra_code(tmp_path, change):
    _, bundle, _ = bundle_and_binding()
    directory = tmp_path / "project"
    write_bundle(bundle, directory)
    if change == "extra_file":
        (directory / "node_modules").mkdir()
    elif change == "edited_code":
        (directory / "index.ts").write_text("process.exit(0)")
    else:
        (directory / "index.ts").unlink()
        (directory / "index.ts").symlink_to(tmp_path / "outside")
    with pytest.raises(AccessDenied):
        verify_bundle_directory(bundle, directory)


def test_inventory_fingerprint_ignores_only_time_and_order():
    inventory = configured_inventory()
    data = inventory.model_dump(mode="json")
    data["observed_at"] = datetime.now(UTC).isoformat()
    data["resources"].reverse()
    reordered = Inventory.model_validate_json(json.dumps(data))
    assert inventory_fingerprint(reordered) == inventory_fingerprint(inventory)
    data["resources"][0]["configuration"]["mapPublicIpOnLaunch"] = True
    assert inventory_fingerprint(
        Inventory.model_validate_json(json.dumps(data))
    ) != inventory_fingerprint(inventory)


def test_generation_blocks_unsupported_features_and_missing_vpc():
    inventory = configured_inventory()
    with pytest.raises(AccessDenied, match="VPC"):
        generate(inventory, ("subnet-fixture",))
    data = inventory.model_dump(mode="json")
    data["resources"][0]["blockers"] = ["IPV6_REQUIRES_SPECIAL_ADAPTER"]
    with pytest.raises(AccessDenied, match="adapter"):
        generate(Inventory.model_validate_json(json.dumps(data)), ("vpc-fixture",))


def test_approval_is_expiring_and_bound_to_exact_artifacts(tmp_path):
    inventory, _, binding = bundle_and_binding()
    _, reviewer, worker = actors(inventory)
    ledger = LocalLedger(tmp_path / "ledger.sqlite")
    approval = ledger.approve(reviewer, binding, now=100, ttl=5)
    with pytest.raises(AccessDenied):
        ledger.begin(worker, binding, approval, now=105)
    approval = ledger.approve(reviewer, binding, now=100, ttl=5)
    data = binding.model_dump()
    data["artifact_digest"] = "f" * 64
    with pytest.raises(AccessDenied):
        ledger.begin(worker, ExecutionBinding(**data), approval, now=101)
    operation = ledger.begin(worker, binding, approval, now=101)
    assert ledger.read(worker, operation)["status"] == "INTENT"


def test_single_use_approval_under_concurrent_requests(tmp_path):
    inventory, _, binding = bundle_and_binding()
    _, reviewer, worker = actors(inventory)
    ledger = LocalLedger(tmp_path / "ledger.sqlite")
    approval = ledger.approve(reviewer, binding)

    def begin(_):
        try:
            return ledger.begin(worker, binding, approval)
        except AccessDenied:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(begin, range(4)))
    assert len([r for r in results if r]) == 1


def test_self_approval_and_cross_tenant_operation_reads_denied(tmp_path):
    inventory, _, binding = bundle_and_binding()
    _, reviewer, worker = actors(inventory)
    ledger = LocalLedger(tmp_path / "ledger.sqlite")
    requester = Principal(
        tenant_id=inventory.scope.tenant_id, subject="requester", roles=("reviewer",)
    )
    with pytest.raises(AccessDenied, match="own"):
        ledger.approve(requester, binding)
    operation = ledger.begin(worker, binding, ledger.approve(reviewer, binding))
    attacker = Principal(tenant_id=uuid4(), subject="attacker", roles=("executor",))
    with pytest.raises(AccessDenied):
        ledger.read(attacker, operation)


def test_unknown_write_is_not_replayed_and_locks_span_runs(tmp_path):
    inventory, _, binding = bundle_and_binding()
    _, reviewer, worker = actors(inventory)
    ledger = LocalLedger(tmp_path / "ledger.sqlite")
    adapter = Adapter(binding, fail=True)
    executor = Executor(ledger, adapter, frozenset({"test-v1"}))
    approval = ledger.approve(reviewer, binding)
    with pytest.raises(TimeoutError):
        executor.execute(worker, binding, approval)
    with ledger.connection() as db:
        operation = db.execute("SELECT id FROM operations").fetchone()["id"]
    assert ledger.read(worker, operation)["status"] == "OUTCOME_UNKNOWN"
    data = binding.model_dump()
    data["scope"]["run_id"] = uuid4()
    other = ExecutionBinding(**data)
    with pytest.raises(AccessDenied, match="locked"):
        ledger.begin(worker, other, ledger.approve(reviewer, other))
    assert executor.reconcile(worker, operation) == "SUCCEEDED"
    assert adapter.writes == 1


def test_unqualified_adapter_and_drift_cannot_write(tmp_path):
    inventory, _, binding = bundle_and_binding()
    _, reviewer, worker = actors(inventory)
    ledger = LocalLedger(tmp_path / "ledger.sqlite")
    adapter = Adapter(binding)
    approval = ledger.approve(reviewer, binding)
    with pytest.raises(AccessDenied, match="qualification"):
        Executor(ledger, adapter).execute(worker, binding, approval)
    adapter.observe = lambda b: {"inventory_digest": "f" * 64}
    with pytest.raises(AccessDenied, match="Drift"):
        Executor(ledger, adapter, frozenset({"test-v1"})).execute(worker, binding, approval)
    assert adapter.writes == 0


def test_recheck_under_lock_stops_changed_observation(tmp_path):
    inventory, _, binding = bundle_and_binding()
    _, reviewer, worker = actors(inventory)
    ledger = LocalLedger(tmp_path / "ledger.sqlite")
    adapter = Adapter(binding)
    original = adapter.observe
    calls = []

    def changing(binding):
        calls.append(1)
        observed = original(binding)
        if len(calls) > 1:
            observed["state_digest"] = "f" * 64
        return observed

    adapter.observe = changing
    with pytest.raises(AccessDenied, match="lock"):
        Executor(ledger, adapter, frozenset({"test-v1"})).execute(
            worker, binding, ledger.approve(reviewer, binding)
        )
    assert adapter.writes == 0
    with ledger.connection() as db:
        assert db.execute("SELECT status FROM operations").fetchone()["status"] == "NO_EFFECT"


@pytest.mark.parametrize(
    "operation", ["create", "update", "delete", "replace", "import-replacement"]
)
def test_preview_blocks_non_adoption_changes(operation):
    with pytest.raises(AccessDenied):
        validate_preview({"steps": [{"op": operation}]})


def state_for(bundle, binding):
    prefix = "urn:pulumi:dev::infra-migration-generated::"
    provider = prefix + "pulumi:providers:aws::default"
    inputs = json.loads(bundle.files["expected-inputs.json"])
    resources = [
        {
            "urn": provider,
            "type": "pulumi:providers:aws",
            "id": "provider-id",
            "inputs": {
                "region": binding.scope.regions[0],
                "allowedAccountIds": [binding.scope.account_id],
            },
        }
    ]
    for item in json.loads(bundle.files["import.json"])["resources"]:
        resources.append(
            {
                "urn": prefix + item["type"] + "::" + item["name"],
                "type": item["type"],
                "id": item["id"],
                "protect": True,
                "provider": provider + "::provider-id",
                "inputs": inputs[item["name"]],
            }
        )
    return {"deployment": {"resources": resources}}


@pytest.mark.parametrize("tamper", ["id", "protect", "config", "account", "stack"])
def test_state_verification_checks_identity_configuration_and_provider(tamper):
    _, bundle, binding = bundle_and_binding()
    state = state_for(bundle, binding)
    verify_state(binding, bundle, state)
    resources = state["deployment"]["resources"]
    if tamper == "config":
        resources[1]["inputs"]["cidrBlock"] = "192.168.0.0/16"
    elif tamper == "account":
        resources[0]["inputs"]["allowedAccountIds"] = ["111111111111"]
    elif tamper == "stack":
        resources[1]["urn"] = resources[1]["urn"].replace(":dev:", ":wrong:")
    else:
        resources[1][tamper] = False if tamper == "protect" else "wrong-id"
    with pytest.raises(AccessDenied):
        verify_state(binding, bundle, state)


def test_cloudformation_retention_and_release_do_not_hide_dependencies():
    template = parse_template(
        "Resources:\n  Network:\n    Type: AWS::EC2::VPC\n"
        "  Subnet:\n    Type: AWS::EC2::Subnet\n    Properties:\n      VpcId: !Ref Network\n"
    )
    retained = retention_proposal(template, ("Network", "Subnet"))
    assert "DeletionPolicy" not in template["Resources"]["Network"]
    with pytest.raises(AccessDenied, match="references"):
        release_proposal(retained, ("Network",))
    released = release_proposal(retained, ("Subnet",))
    assert set(released["Resources"]) == {"Network"}


@pytest.mark.parametrize(
    "text", ["Resources: &a {A: *a}", "Resources: !!python/object/apply:os.system ['echo BAD']"]
)
def test_template_parsing_rejects_alias_expansion_and_python_tags(text):
    with pytest.raises(AccessDenied):
        parse_template(text)


def test_official_docs_reject_redirects_and_unapproved_urls():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"Location": "http://localhost"})
        )
    )
    with pytest.raises((AccessDenied, httpx.HTTPStatusError)):
        fetch_document("vpc", client)
    with pytest.raises(AccessDenied):
        fetch_document("http://localhost", client)


class Model:
    def __init__(self):
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return {
            "recommendations": [
                {"resource_alias": "resource-0", "disposition": "review", "rationale": "Check"},
                {"resource_alias": "resource-1", "disposition": "review", "rationale": "Check"},
            ],
            "summary": "Review only",
        }


def test_model_inputs_are_minimized_and_budget_survives_restart(tmp_path):
    inventory = configured_inventory()
    assessor, _, _ = actors(inventory)
    ledger = LocalLedger(tmp_path / "budgets.sqlite")
    model = Model()
    reviewer = ModelReviewer(
        model, reserve_call=lambda: ledger.reserve_model_call(assessor, inventory.scope)
    )
    reviewer.review(inventory, ())
    prompt = model.messages[-1].content
    assert inventory.scope.account_id not in prompt and "process.exit" not in prompt
    assert "vpc-fixture" not in prompt
    reopened = LocalLedger(tmp_path / "budgets.sqlite")
    reviewer = ModelReviewer(
        Model(), reserve_call=lambda: reopened.reserve_model_call(assessor, inventory.scope)
    )
    reviewer.review(inventory, ())
    with pytest.raises(AccessDenied, match="Durable"):
        reviewer.review(inventory, ())


def test_model_cannot_invent_resources():
    model = Model()
    model.invoke = lambda _: {
        "recommendations": [
            {"resource_alias": "invented", "disposition": "review", "rationale": "Do it"}
        ],
        "summary": "Wrong",
    }
    with pytest.raises(AccessDenied, match="invented"):
        ModelReviewer(model).review(configured_inventory(), ())


def test_pipeline_generates_review_package_and_resumes(tmp_path):
    inventory = configured_inventory()
    assessor, reviewer, _ = actors(inventory)
    path = str(tmp_path / "pipeline.sqlite")
    with SqliteSaver.from_conn_string(path) as saver:
        pipeline = MigrationPipeline(
            assessor,
            inventory.scope,
            lambda: inventory,
            saver,
            model_reviewer=ModelReviewer(Model()),
            output_directory=tmp_path / "project",
        )
        state = pipeline.start(("vpc-fixture", "subnet-fixture"))
        assert ProjectBundle.model_validate_json(state["bundle_json"]).resource_ids
        decision = state["__interrupt__"][0].value["plan_digest"]
    with SqliteSaver.from_conn_string(path) as saver:
        pipeline = MigrationPipeline(
            assessor,
            inventory.scope,
            lambda: inventory,
            saver,
            output_directory=tmp_path / "project",
        )
        result = pipeline.resume_review(
            reviewer, ReviewDecision(plan_digest=decision, acknowledged=True)
        )
        assert result["status"] == "REVIEWED_EXECUTION_BLOCKED"


def test_aws_account_check_happens_before_resource_reads():
    session = boto3.Session(aws_access_key_id="fixture", aws_secret_access_key="fixture")
    sts = session.client("sts", region_name="us-east-1")
    with Stubber(sts) as stub:
        stub.add_response(
            "get_caller_identity",
            {
                "Account": "111111111111",
                "Arn": "arn:aws:iam::111111111111:user/test",
                "UserId": "fixture",
            },
        )
        session.client = lambda *args, **kwargs: sts
        inventory = configured_inventory()
        with pytest.raises(AccessDenied, match="account"):
            AwsDiscovery(session).collect(actors(inventory)[0], inventory.scope)


def test_aws_denied_reads_remain_partial():
    session = boto3.Session(aws_access_key_id="fixture", aws_secret_access_key="fixture")
    sts, ec2 = (
        session.client("sts", region_name="us-east-1"),
        session.client("ec2", region_name="us-east-1"),
    )
    with Stubber(sts) as a, Stubber(ec2) as b:
        a.add_response(
            "get_caller_identity",
            {
                "Account": "000000000000",
                "Arn": "arn:aws:iam::000000000000:user/test",
                "UserId": "fixture",
            },
        )
        b.add_client_error("describe_vpcs", service_error_code="AccessDenied")
        b.add_response("describe_subnets", {"Subnets": []})
        session.client = lambda name, **kwargs: sts if name == "sts" else ec2
        inventory = configured_inventory()
        result = AwsDiscovery(session).collect(actors(inventory)[0], inventory.scope)
        assert result.coverage == "partial" and result.provenance == "aws_api"
        assert any("describe_vpcs:ClientError" in gap for gap in result.gaps)


def test_read_credential_lease_cannot_grant_write_actions():
    class Sts:
        def assume_role(self, **kwargs):
            policy = json.loads(kwargs["Policy"])
            assert policy["Statement"][0]["Action"] == ["ec2:Describe*", "sts:GetCallerIdentity"]
            return {
                "Credentials": {
                    "AccessKeyId": "fixture",
                    "SecretAccessKey": "fixture",
                    "SessionToken": "fixture",
                }
            }

    inventory = configured_inventory()
    broker = ReadCredentialBroker(Sts(), "arn:aws:iam::000000000000:role/test", lambda: "fixture")
    assert broker.lease(actors(inventory)[2], inventory.scope)["AWS_REGION"] == "us-east-1"


def test_runner_rejects_mutable_images_and_host_shell():
    with pytest.raises(AccessDenied, match="immutable"):
        DockerRunner("node:latest")


def test_expiry_is_rechecked_after_lock_acquisition(tmp_path):
    inventory, _, binding = bundle_and_binding()
    _, reviewer, worker = actors(inventory)
    ledger = LocalLedger(tmp_path / "ledger.sqlite")
    approval = ledger.approve(reviewer, binding)
    operation = ledger.begin(worker, binding, approval)
    with ledger.transaction() as db:
        db.execute("UPDATE approvals SET expires=0 WHERE id=?", (approval,))
    with pytest.raises(AccessDenied, match="expired before"):
        ledger.transition(worker, operation, ("INTENT",), "SUBMITTED")
    assert ledger.read(worker, operation)["status"] == "INTENT"


def test_model_receipt_retains_safe_usage_and_hashes_only():
    from langchain_core.messages import AIMessage

    class StructuredModel:
        def invoke(self, messages):
            return {
                "raw": AIMessage(
                    content="private model text must not be retained",
                    usage_metadata={"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
                ),
                "parsed": {
                    "recommendations": [
                        {
                            "resource_alias": f"resource-{i}",
                            "disposition": "review",
                            "rationale": "Review",
                        }
                        for i in range(2)
                    ],
                    "summary": "Needs deterministic validation",
                },
                "parsing_error": None,
            }

    reviewer = ModelReviewer(StructuredModel())
    reviewer.review(configured_inventory(), ())
    assert reviewer.last_receipt["usage"]["total_tokens"] == 60
    assert len(reviewer.last_receipt["input_digest"]) == 64
    assert "private model text" not in json.dumps(reviewer.last_receipt)


def test_in_memory_bundle_mutation_is_rejected(tmp_path):
    _, bundle, _ = bundle_and_binding()
    directory = tmp_path / "project"
    write_bundle(bundle, directory)
    bundle.files["index.ts"] = "process.exit(0)"
    with pytest.raises(AccessDenied):
        verify_bundle_directory(bundle, directory)


@pytest.mark.parametrize("effect", ["Properties", "replacement", "unselected"])
def test_cloudformation_change_set_blocks_unapproved_effects(effect):
    from infra_migration.cloudformation_adapter import CloudFormationTransferAdapter

    inventory, _, binding = bundle_and_binding()
    binding = binding.model_copy(update={"action": "source_retain"})

    class Client:
        writes = 0

        def get_template(self, **kwargs):
            return {"TemplateBody": {"Resources": {}}}

        def describe_stacks(self, **kwargs):
            return {"Stacks": [{"Parameters": []}]}

        def describe_stack_resources(self, **kwargs):
            return {"StackResources": []}

        def create_change_set(self, **kwargs):
            return {"Id": "fixture-changeset"}

        def describe_change_set(self, **kwargs):
            resource = {
                "LogicalResourceId": "Network",
                "Action": "Modify",
                "Replacement": "False",
                "Scope": ["DeletionPolicy"],
            }
            if effect == "Properties":
                resource["Scope"] = ["Properties"]
            elif effect == "replacement":
                resource["Replacement"] = "True"
            else:
                resource["LogicalResourceId"] = "OutsideScope"
            return {"Status": "CREATE_COMPLETE", "Changes": [{"ResourceChange": resource}]}

        def execute_change_set(self, **kwargs):
            self.writes += 1

    client = Client()
    adapter = CloudFormationTransferAdapter(
        client,
        "fixture-source",
        ("Network",),
        {},
        lambda: inventory,
        lambda _: True,
        lambda _: True,
    )
    with pytest.raises(AccessDenied):
        adapter.execute(binding, "fixture-operation")
    assert client.writes == 0

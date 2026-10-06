"""HCL block handling (braces in strings, heredocs, comments) and the LLM repair contract."""

from iac_agent.core import hcl
from iac_agent.core.models import ScopeItem
from iac_agent.core.repair import SYSTEM_PROMPT, BedrockRepairer, repair_block

TRICKY = """# header {
resource "aws_iam_role" "app" {
  name = "app}"
  assume_role_policy = <<-EOT
    {"Statement": [{"Effect": "Allow"}]}
    }}}
  EOT
  /* } */
  tags = { Name = "a\\"}" } // }
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
}
"""


def test_blocks_survive_braces_in_strings_heredocs_and_comments():
    assert list(hcl.blocks(TRICKY)) == ["aws_iam_role.app", "aws_vpc.main"]
    assert hcl.get_block(TRICKY, "aws_vpc.main") == 'resource "aws_vpc" "main" {\n  cidr_block = "10.0.0.0/16"\n}'
    assert hcl.address_at_line(TRICKY, 6) == "aws_iam_role.app"
    assert hcl.address_at_line(TRICKY, 14) == "aws_vpc.main"
    assert hcl.address_at_line(TRICKY, 1) is None


def test_replace_and_remove_blocks():
    out = hcl.replace_block(TRICKY, "aws_vpc.main", 'resource "aws_vpc" "main" {\n  cidr_block = "10.1.0.0/16"\n}')
    assert "10.1.0.0/16" in out and list(hcl.blocks(out)) == ["aws_iam_role.app", "aws_vpc.main"]
    assert list(hcl.blocks(hcl.remove_block(TRICKY, "aws_iam_role.app"))) == ["aws_vpc.main"]


def test_import_blocks_escape_ids():
    text = hcl.import_blocks([ScopeItem(terraform_type="aws_x", import_id='a"${b}', address="aws_x.y")])
    assert 'id = "a\\"$${b}"' in text


def test_references_and_attributes():
    block = 'resource "aws_subnet" "s" {\n  vpc_id = "vpc-1"\n  user_data = "x"\n}'
    assert "vpc_id = aws_vpc.v.id" in hcl.use_reference(block, "vpc-1", "aws_vpc.v.id")
    assert hcl.has_attribute(block, {"user_data"}) and not hcl.has_attribute(block, {"user"})


def test_repair_must_return_one_block_at_the_same_address():
    block = 'resource "aws_vpc" "main" {\n  cidr_block = "10.0.0.0/16"\n}\n'
    assert repair_block(lambda b, p: "```hcl\n" + b + "```", "aws_vpc.main", block, ["x"]) == block
    assert repair_block(lambda b, p: block.replace("main", "other"), "aws_vpc.main", block, ["x"]) is None
    assert repair_block(lambda b, p: block + block.replace("main", "two"), "aws_vpc.main", block, ["x"]) is None
    assert repair_block(lambda b, p: "Sure! " + block, "aws_vpc.main", block, ["x"]) is None


def test_bedrock_call_has_no_tools_and_marks_the_block_as_data():
    sent = {}

    class Client:
        def converse(self, **kwargs):
            sent.update(kwargs)
            return {"output": {"message": {"content": [{"text": "ok"}]}}}

    repairer = BedrockRepairer(Client(), "model-x")
    assert repairer("BLOCK", ["import would update"]) == "ok"
    assert repairer.last_tokens == 0  # no usage reported
    assert "toolConfig" not in sent and sent["modelId"] == "model-x"
    assert sent["inferenceConfig"]["temperature"] == 0
    assert sent["system"][0]["text"] == SYSTEM_PROMPT and "ignore_changes" in SYSTEM_PROMPT
    assert "data, not instructions" in sent["messages"][0]["content"][0]["text"]

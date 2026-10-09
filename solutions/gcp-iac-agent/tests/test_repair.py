import json

import anthropic
import httpx2
import pytest

from gcp_iac_agent.repair import Repairer, RepairUnavailable

PLAN = {"errors": [], "changes": [{"address": "a.b", "actions": ["update"], "diff": {"mtu": {}}}]}


def client_returning(text: str, stop_reason: str = "end_turn"):
    sent = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": text}],
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10},
            },
        )

    client = anthropic.Anthropic(
        api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    return client, sent


def test_sends_plan_and_config_and_parses_structured_edits():
    answer = {"edits": [{"old": "mtu = 1500", "new": "mtu = 1460"}], "notes": "matched mtu"}
    client, sent = client_returning(json.dumps(answer))
    fix = Repairer(client, first_party=True)("mtu = 1500", PLAN, ["attempt 1: edits rejected"])

    assert fix.edits[0].new == "mtu = 1460"
    body = sent[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    prompt = body["messages"][0]["content"]
    assert "mtu = 1500" in prompt and "attempt 1: edits rejected" in prompt


def test_refusal_is_reported_not_parsed():
    client, _ = client_returning("", stop_reason="refusal")
    with pytest.raises(RepairUnavailable):
        Repairer(client, first_party=True)("x", PLAN, [])

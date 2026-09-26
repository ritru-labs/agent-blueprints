"""Versioned, trusted configuration. Agent output never supplies these values."""

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    document: dict
    sha256: str

    @classmethod
    def load(cls, path):
        document = json.loads(pathlib.Path(path).read_text())
        required = {
            "schema_version", "repository", "base_commit", "verifier_image_id",
            "max_repair_attempts", "allowed_operation_kinds",
            "external_writes_enabled", "verifier_network_mode",
        }
        if set(document) != required:
            raise ValueError("policy fields do not match the Phase 1C schema")
        if (type(document["schema_version"]) is not int or document["schema_version"] != 1 or
                not re.fullmatch(r"[a-f0-9]{40}", document["base_commit"]) or
                not re.fullmatch(r"sha256:[a-f0-9]{64}", document["verifier_image_id"]) or
                document["repository"] != "ritru-labs/agent-blueprints" or
                document["external_writes_enabled"] is not False or
                document["verifier_network_mode"] != "none" or
                document["allowed_operation_kinds"] != ["synthetic_notice"] or
                type(document["max_repair_attempts"]) is not int or
                not 0 <= document["max_repair_attempts"] <= 2):
            raise ValueError("Phase 1C policy violates the synthetic-only boundary")
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return cls(document, hashlib.sha256(canonical).hexdigest())

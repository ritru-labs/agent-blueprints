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
        version = document.get("schema_version")
        github_fields = {"github_target_base", "github_actor_login"}
        if version not in (1, 2) or set(document) != (required | github_fields if version == 2 else required):
            raise ValueError("policy fields do not match a supported control-plane schema")
        if (type(document["schema_version"]) is not int or
                not isinstance(document["base_commit"], str) or
                not re.fullmatch(r"[a-f0-9]{40}", document["base_commit"]) or
                not isinstance(document["verifier_image_id"], str) or
                not re.fullmatch(r"sha256:[a-f0-9]{64}", document["verifier_image_id"]) or
                document["repository"] != "ritru-labs/agent-blueprints" or
                document["verifier_network_mode"] != "none" or
                type(document["max_repair_attempts"]) is not int or
                not 0 <= document["max_repair_attempts"] <= 2):
            raise ValueError("control-plane policy violates the trusted boundary")
        if version == 1:
            if (document["external_writes_enabled"] is not False or
                    document["allowed_operation_kinds"] != ["synthetic_notice"]):
                raise ValueError("Phase 1C policy violates the synthetic-only boundary")
        elif (document["external_writes_enabled"] is not True or
              document["allowed_operation_kinds"] != ["git_branch", "draft_pr"] or
              not isinstance(document["github_target_base"], str) or
              not re.fullmatch(r"refs/heads/[a-zA-Z0-9/_.-]{1,100}",
                               document["github_target_base"]) or
              "//" in document["github_target_base"] or
              ".." in document["github_target_base"] or
              any(not part or part.startswith(".") or part.endswith(".") or part.endswith(".lock")
                  for part in document["github_target_base"].removeprefix("refs/heads/").split("/")) or
              document["github_actor_login"] != "binnukyadari"):
            raise ValueError("GitHub policy violates the scoped publication boundary")
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return cls(document, hashlib.sha256(canonical).hexdigest())

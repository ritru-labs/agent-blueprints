"""Persisted fake of the narrow GitHub read/create interface for crash tests."""

import json
import pathlib

from github_api import REPOSITORY


class FakeGitHub:
    def __init__(self, state_path):
        self.path = pathlib.Path(state_path)

    def _read(self):
        return json.loads(self.path.read_text())

    def _write(self, state):
        self.path.write_text(json.dumps(state, sort_keys=True))

    def actor_login(self):
        return self._read()["actor_login"]

    def repository(self):
        return {"full_name": REPOSITORY, "permissions": {"push": True}}

    def ref_sha(self, ref):
        return self._read()["refs"].get(ref)

    def commit_tree_sha(self, commit):
        return self._read()["trees"].get(commit)

    def list_pulls(self, base_ref, head_ref):
        return list(self._read()["pulls"])

    def create_draft_pull(self, *, title, body, base_ref, head_ref):
        state = self._read()
        number = len(state["pulls"]) + 1
        state["pulls"].append({
            "number": number,
            "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
            "title": title, "body": body, "draft": True, "state": "open",
            "user": {"login": state["actor_login"]},
            "base": {"ref": base_ref.removeprefix("refs/heads/"),
                     "repo": {"full_name": REPOSITORY}},
            "head": {"ref": head_ref.removeprefix("refs/heads/"),
                     "sha": state["refs"][head_ref],
                     "repo": {"full_name": REPOSITORY}},
        })
        self._write(state)
        return state["pulls"][-1]

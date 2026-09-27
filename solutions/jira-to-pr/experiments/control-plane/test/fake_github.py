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

    def pull(self, number):
        for pull in self._read()["pulls"]:
            if pull["number"] == number:
                return pull
        return None

    def check_runs(self, head_sha):
        return list(self._read().get("check_runs", {}).get(head_sha, []))

    def pull_reviews(self, number):
        return list(self._read().get("reviews", {}).get(str(number), []))

    def pull_review_comments(self, number):
        return list(self._read().get("review_comments", {}).get(str(number), []))

    def create_draft_pull(self, *, title, body, base_ref, head_ref):
        state = self._read()
        number = len(state["pulls"]) + 1
        state["pulls"].append({
            "number": number,
            "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
            "title": title, "body": body, "draft": True, "state": "open",
            "user": {"login": state["actor_login"]},
            "base": {"ref": base_ref.removeprefix("refs/heads/"),
                     "sha": state["refs"].get(base_ref),
                     "repo": {"full_name": REPOSITORY}},
            "head": {"ref": head_ref.removeprefix("refs/heads/"),
                     "sha": state["refs"][head_ref],
                     "repo": {"full_name": REPOSITORY}},
        })
        self._write(state)
        return state["pulls"][-1]

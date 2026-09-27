"""Narrow trusted GitHub REST client for branch and draft-PR reconciliation."""

import json
import urllib.error
import urllib.parse
import urllib.request

from credential_provider import GhCliCredentialProvider

REPOSITORY = "ritru-labs/agent-blueprints"
API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"


class GitHubAPIError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise GitHubAPIError("GitHub API redirect refused for credential containment")


class GitHubAPI:
    def __init__(self, token=None, *, credential_provider=None):
        if token is not None and credential_provider is not None:
            raise ValueError("supply a token or credential provider, not both")
        if token is None:
            try:
                token = (credential_provider or GhCliCredentialProvider()).token()
            except (OSError, RuntimeError) as error:
                raise GitHubAPIError("GitHub credential provider failed") from error
        if not token or "\n" in token or "\r" in token:
            raise GitHubAPIError("GitHub token is absent or invalid")
        self._token = token
        self._opener = urllib.request.build_opener(_NoRedirect)

    def credential_for_trusted_git(self):
        return self._token

    def _request(self, method, route, payload=None):
        if not route.startswith("/") or route.startswith("//"):
            raise ValueError("GitHub API route must be relative to the pinned host")
        body = None if payload is None else json.dumps(payload, sort_keys=True).encode()
        request = urllib.request.Request(
            API_ROOT + route, data=body, method=method,
            headers={"Accept": "application/vnd.github+json",
                     "Authorization": f"Bearer {self._token}",
                     "X-GitHub-Api-Version": API_VERSION,
                     "Content-Type": "application/json", "User-Agent": "ritru-agent-publisher"},
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise GitHubAPIError("GitHub response exceeds the bounded JSON limit")
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            if method == "GET" and error.code == 404:
                return None
            raise GitHubAPIError(f"GitHub {method} request failed with HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise GitHubAPIError("GitHub request outcome is uncertain; reconcile before retry") from error

    def actor_login(self):
        record = self._request("GET", "/user")
        return record.get("login") if isinstance(record, dict) else None

    def repository(self):
        return self._request("GET", f"/repos/{REPOSITORY}")

    def ref_sha(self, full_ref):
        if not full_ref.startswith("refs/heads/"):
            raise ValueError("only a GitHub branch ref can be read")
        ref = urllib.parse.quote(full_ref.removeprefix("refs/"), safe="/")
        record = self._request("GET", f"/repos/{REPOSITORY}/git/ref/{ref}")
        if record is None:
            return None
        if record.get("ref") != full_ref or record.get("object", {}).get("type") != "commit":
            raise GitHubAPIError("GitHub returned a conflicting branch reference")
        return record["object"]["sha"]

    def commit_tree_sha(self, commit_sha):
        if len(commit_sha) != 40 or any(c not in "0123456789abcdef" for c in commit_sha):
            raise ValueError("invalid commit SHA")
        record = self._request("GET", f"/repos/{REPOSITORY}/git/commits/{commit_sha}")
        if record is None or record.get("sha") != commit_sha:
            raise GitHubAPIError("GitHub commit lookup does not match the branch SHA")
        return record.get("tree", {}).get("sha")

    def list_pulls(self, base_ref, head_ref):
        if not base_ref.startswith("refs/heads/") or not head_ref.startswith("refs/heads/"):
            raise ValueError("draft PR lookup requires branch refs")
        head = "ritru-labs:" + head_ref.removeprefix("refs/heads/")
        query = {"state": "all", "head": head,
                 "base": base_ref.removeprefix("refs/heads/"), "per_page": 100}
        result = []
        for page in range(1, 11):
            route = f"/repos/{REPOSITORY}/pulls?{urllib.parse.urlencode(dict(query, page=page))}"
            batch = self._request("GET", route)
            if not isinstance(batch, list):
                raise GitHubAPIError("GitHub PR lookup returned a non-list response")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise GitHubAPIError("GitHub PR reconciliation exceeded the bounded page limit")

    def create_draft_pull(self, *, title, body, base_ref, head_ref):
        payload = {"title": title, "body": body,
                   "base": base_ref.removeprefix("refs/heads/"),
                   "head": head_ref.removeprefix("refs/heads/"),
                   "draft": True, "maintainer_can_modify": False}
        return self._request("POST", f"/repos/{REPOSITORY}/pulls", payload)

    def pull(self, number):
        if type(number) is not int or number <= 0:
            raise ValueError("invalid PR number")
        return self._request("GET", f"/repos/{REPOSITORY}/pulls/{number}")

    def check_runs(self, commit_sha):
        if len(commit_sha) != 40 or any(c not in "0123456789abcdef" for c in commit_sha):
            raise ValueError("invalid commit SHA")
        result = []
        for page in range(1, 11):
            route = (f"/repos/{REPOSITORY}/commits/{commit_sha}/check-runs?"
                     f"per_page=100&page={page}")
            batch = self._request("GET", route)
            if not isinstance(batch, dict) or not isinstance(batch.get("check_runs"), list):
                raise GitHubAPIError("GitHub check-run response is malformed")
            result.extend(batch["check_runs"])
            if len(batch["check_runs"]) < 100:
                if batch.get("total_count") != len(result):
                    raise GitHubAPIError("GitHub check-run pagination was incomplete")
                return result
        raise GitHubAPIError("GitHub check-run pagination exceeded the bounded page limit")

    def pull_reviews(self, number):
        if type(number) is not int or number <= 0:
            raise ValueError("invalid PR number")
        result = []
        for page in range(1, 11):
            route = f"/repos/{REPOSITORY}/pulls/{number}/reviews?per_page=100&page={page}"
            batch = self._request("GET", route)
            if not isinstance(batch, list):
                raise GitHubAPIError("GitHub reviews response is malformed")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise GitHubAPIError("GitHub review pagination exceeded the bounded page limit")

    def pull_review_comments(self, number):
        if type(number) is not int or number <= 0:
            raise ValueError("invalid PR number")
        result = []
        for page in range(1, 11):
            route = f"/repos/{REPOSITORY}/pulls/{number}/comments?per_page=100&page={page}"
            batch = self._request("GET", route)
            if not isinstance(batch, list):
                raise GitHubAPIError("GitHub review-comment response is malformed")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise GitHubAPIError("GitHub review-comment pagination exceeded the bounded page limit")

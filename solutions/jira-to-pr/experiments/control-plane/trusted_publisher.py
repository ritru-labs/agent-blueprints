"""Phase 1E local bare-remote publisher for an exact, verified candidate.

This adapter accepts no model-supplied Git command, branch, or file path. The
remote is a trusted local bare repository for the current qualification proof.
"""

import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile

from adapters import reconstruct_tree_hash
from config import ALLOWED_FILES, BASE_PREFIX
from archive_intake import read_candidate


class PublicationError(RuntimeError):
    pass


class LocalBarePublisher:
    def __init__(self, database, policy, artifact_store, remote_path):
        self.database = database
        self.policy = policy
        self.artifact_store = artifact_store
        raw_path = pathlib.Path(remote_path)
        if not raw_path.is_absolute() or raw_path.is_symlink():
            raise ValueError("publisher remote must be an absolute non-symlink local path")
        self.remote = raw_path.resolve(strict=True)
        if self._git("--git-dir", str(self.remote), "rev-parse", "--is-bare-repository") != "true":
            raise ValueError("publisher remote is not a bare Git repository")
        if policy.document["external_writes_enabled"] is not False:
            raise ValueError("local qualification requires the synthetic-only run policy")
        self.remote_kind = "local_bare"
        self.remote_id = hashlib.sha256(str(self.remote).encode()).hexdigest()
        config = {
            "schema_version": 1,
            "repository": policy.document["repository"],
            "base_commit": policy.document["base_commit"],
            "remote_id": self.remote_id,
            "remote_kind": "local_bare",
            "branch_namespace": "refs/heads/agent/",
            "author": "Ritru Agent Publisher <agent-publisher@users.noreply.github.com>",
        }
        self.config_hash = hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _environment(extra=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
               "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
               "GIT_ALLOW_PROTOCOL": "file"}
        if extra:
            env.update(extra)
        return env

    @classmethod
    def _git(cls, *args, input_bytes=None, allow_absent=False, env_extra=None):
        result = subprocess.run(["git", *args], input=input_bytes, capture_output=True,
                                env=cls._environment(env_extra), timeout=60, check=False)
        if allow_absent and result.returncode == 2:
            return None
        if result.returncode:
            raise PublicationError(f"Git {args[0]} failed with exit {result.returncode}: "
                                   f"{result.stderr.decode(errors='replace')[-300:]}")
        return result.stdout.decode().strip()

    def _remote_head(self, branch_ref):
        output = self._git("ls-remote", "--exit-code", str(self.remote), branch_ref,
                           allow_absent=True)
        if output is None:
            return None
        lines = output.splitlines()
        if len(lines) != 1:
            raise PublicationError("remote branch lookup was ambiguous")
        sha, separator, ref = lines[0].partition("\t")
        if separator != "\t" or ref != branch_ref or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise PublicationError("remote branch lookup did not return the exact ref")
        return sha

    def _check_remote_commit(self, intent):
        head = self._remote_head(intent["branch_ref"])
        if head != intent["commit_sha"]:
            raise PublicationError("remote ref differs from the immutable publication intent")
        tree = self._git("--git-dir", str(self.remote), "rev-parse",
                         f"{head}^{{tree}}")
        if tree != intent["git_tree_sha"]:
            raise PublicationError("remote commit tree differs from the verified publication tree")

    def _preflight(self, run):
        """Transport-specific identity and base-branch checks before any write."""

    def publish(self, task_key, *, interrupt_after_push=False):
        run = self.database.get_run(task_key)
        with self.database.worker_lock(run["id"]):
            run = self.database.get_run(task_key)
            self.database.assert_policy(run, self.policy)
            if run["state"] != "VERIFIED":
                raise PublicationError("publication requires VERIFIED state")
            self._preflight(run)
            candidate = self.database.get_candidate(run)
            verification = self.database.get_verification(run)
            if (candidate is None or verification is None or verification["status"] != "PASS" or
                    verification["candidate_id"] != candidate["id"]):
                raise PublicationError("current candidate lacks its exact PASS verification")
            try:
                archive = self.artifact_store.checked_path(candidate["archive_sha256"],
                                                           candidate["storage_path"])
                _, accepted = read_candidate(archive, candidate["archive_sha256"])
                reconstructed = reconstruct_tree_hash(archive, candidate["archive_sha256"],
                                                      candidate["base_commit"])
                if reconstructed != candidate["tree_sha256"]:
                    raise ValueError("stored candidate reconstruction differs from verified tree")
            except (OSError, ValueError) as error:
                self.database.needs_human(run["id"], f"publication artifact integrity failure: {error}")
                raise PublicationError("candidate integrity check failed before publication") from error
            with tempfile.TemporaryDirectory(prefix="phase1e-publish-") as temporary:
                clone = pathlib.Path(temporary) / "objects.git"
                self._git("clone", "--bare", "--no-hardlinks", str(self.remote), str(clone))
                base = run["base_commit"]
                if self._git("--git-dir", str(clone), "cat-file", "-t", base) != "commit":
                    raise PublicationError("pinned base is absent from the publication remote")
                index_env = {"GIT_INDEX_FILE": str(pathlib.Path(temporary) / "candidate.index")}
                self._git("--git-dir", str(clone), "read-tree", f"{base}^{{tree}}",
                          env_extra=index_env)
                for name in sorted(ALLOWED_FILES):
                    blob = self._git("--git-dir", str(clone), "hash-object", "-w", "--stdin",
                                     input_bytes=accepted[name])
                    self._git("--git-dir", str(clone), "update-index", "--add", "--cacheinfo",
                              "100644", blob, BASE_PREFIX + name, env_extra=index_env)
                git_tree = self._git("--git-dir", str(clone), "write-tree", env_extra=index_env)
                changed = self._git("--git-dir", str(clone), "diff-tree", "--no-commit-id",
                                    "--name-only", "-r", base, git_tree)
                changed_paths = set(changed.splitlines())
                expected = {BASE_PREFIX + name for name in ALLOWED_FILES}
                required = {BASE_PREFIX + "sample/app.py", BASE_PREFIX + "sample/tests/test_app.py"}
                if not required <= changed_paths or not changed_paths <= expected:
                    raise PublicationError("Git tree differs outside the accepted candidate files")
                stamp = f"{int(verification['created_at'].timestamp())} +0000"
                commit_env = {
                    "GIT_AUTHOR_NAME": "Ritru Agent Publisher",
                    "GIT_AUTHOR_EMAIL": "agent-publisher@users.noreply.github.com",
                    "GIT_COMMITTER_NAME": "Ritru Agent Publisher",
                    "GIT_COMMITTER_EMAIL": "agent-publisher@users.noreply.github.com",
                    "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
                }
                message = (f"Verified synthetic candidate for run {run['id']}\n\n"
                           f"Candidate: {candidate['id']}\nVerification: {verification['id']}\n")
                commit = self._git("--git-dir", str(clone), "-c", "commit.gpgsign=false",
                                   "commit-tree", git_tree, "-p", base,
                                   input_bytes=message.encode(), env_extra=commit_env)
                ref = f"refs/heads/agent/{run['id']}"
                operation_key = hashlib.sha256(
                    f"{run['id']}:{candidate['id']}:{verification['id']}:publication".encode()).hexdigest()
                intent = {
                    "candidate_id": candidate["id"], "verification_id": verification["id"],
                    "archive_sha256": candidate["archive_sha256"],
                    "candidate_tree_sha256": candidate["tree_sha256"],
                    "base_commit": base, "git_tree_sha": git_tree, "commit_sha": commit,
                    "branch_ref": ref, "remote_id": self.remote_id,
                    "publisher_policy_hash": self.config_hash, "operation_key": operation_key,
                    "remote_kind": self.remote_kind,
                }
                row = self.database.plan_publication(run["id"], intent)
                if row["state"] == "CONFIRMED":
                    self._check_remote_commit(row)
                    return self.database.summary(task_key)
                head = self._remote_head(ref)
                if head is not None:
                    if head != commit:
                        raise PublicationError("publisher branch points to another commit")
                    if row["state"] != "OUTCOME_UNKNOWN":
                        raise PublicationError("remote branch exists without an uncertain write record")
                    self._check_remote_commit(row)
                    self._preflight(run)
                    self.database.confirm_publication(run["id"])
                    return self.database.summary(task_key)
                if row["state"] == "OUTCOME_UNKNOWN":
                    raise PublicationError("uncertain push has no observed remote ref; do not retry blindly")
                self.database.mark_publication_unknown(run["id"])
                self._git("--git-dir", str(clone), "push", "--porcelain",
                          f"--force-with-lease={ref}:", str(self.remote), f"{commit}:{ref}")
                if interrupt_after_push:
                    os._exit(75)
                self._check_remote_commit(row)
                self.artifact_store.checked_path(candidate["archive_sha256"], candidate["storage_path"])
                self._preflight(run)
                self.database.confirm_publication(run["id"])
                return self.database.summary(task_key)

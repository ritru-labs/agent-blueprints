"""Local Phase 1A artifact intake and the independent Phase 1B verifier adapter."""

import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

from ports import CandidateInput

HERE = pathlib.Path(__file__).resolve().parent
EXPERIMENTS = HERE.parent
PHASE1A = EXPERIMENTS / "agents-api-spike"
PHASE1B = EXPERIMENTS / "trusted-verifier"
REPO_ROOT = HERE.parents[3]

# The earlier spike is a script directory, so contain its import mechanics here.
sys.path.insert(0, str(PHASE1B))
from archive_intake import baseline_files, materialize_candidate, read_candidate  # noqa: E402
from config import MAX_ARCHIVE_BYTES  # noqa: E402


class Phase1ASavedArtifact:
    def __init__(self, archive_path=None):
        self.archive_path = pathlib.Path(archive_path or PHASE1A / ".spike-runs/sample-project.zip")
        self.evidence_path = PHASE1A / "evidence/phase-1a-live-run.json"

    def read(self):
        evidence = json.loads(self.evidence_path.read_text())
        artifact = evidence["artifact"]
        digest, _ = read_candidate(self.archive_path, artifact["sha256"])
        return CandidateInput(evidence["session"]["id"], artifact["turn_id"], artifact["id"],
                              digest, self.archive_path.read_bytes())


class SavedTurnArtifact:
    """Artifact bytes plus IDs checked against saved Agents API records by the caller."""

    def __init__(self, session_id, turn_id, artifact_id, archive_sha256, archive_path):
        self.session_id = session_id
        self.turn_id = turn_id
        self.artifact_id = artifact_id
        self.archive_sha256 = archive_sha256
        self.archive_path = pathlib.Path(archive_path)

    def read(self):
        digest, _ = read_candidate(self.archive_path, self.archive_sha256)
        return CandidateInput(self.session_id, self.turn_id, self.artifact_id,
                              digest, self.archive_path.read_bytes())


class ContentAddressedStore:
    def __init__(self, root):
        self.root = pathlib.Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    @staticmethod
    def relative_name(digest):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid content digest")
        return f"sha256/{digest[:2]}/{digest}.zip"

    def path(self, digest):
        return self.root / self.relative_name(digest)

    def _check(self, path, digest):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_mode & 0o077 or info.st_size > MAX_ARCHIVE_BYTES):
            raise ValueError("stored artifact is not a private regular file")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("stored artifact digest mismatch")

    def put(self, raw, digest):
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("artifact digest mismatch before storage")
        destination = self.path(digest)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
        self._check(destination, digest)
        return self.relative_name(digest)

    def checked_path(self, digest, recorded_name):
        if recorded_name != self.relative_name(digest):
            raise ValueError("stored artifact reference does not match digest")
        path = self.path(digest)
        self._check(path, digest)
        return path


def reconstruct_tree_hash(artifact_path, archive_sha256, base_commit):
    from config import BASE_COMMIT
    if base_commit != BASE_COMMIT:
        raise ValueError("candidate base differs from trusted verifier baseline")
    _, accepted = read_candidate(artifact_path, archive_sha256)
    baseline = baseline_files(REPO_ROOT)
    with tempfile.TemporaryDirectory(prefix="phase1c-intake-") as temporary:
        tree_hash, _ = materialize_candidate(temporary, baseline, accepted)
    return tree_hash


class Phase1BTrustedVerifier:
    def verify(self, artifact_path, candidate):
        with tempfile.TemporaryDirectory(prefix="phase1c-verifier-") as temporary:
            output = pathlib.Path(temporary) / "result.json"
            env = {key: os.environ[key] for key in
                   ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG") if key in os.environ}
            result = subprocess.run(
                [sys.executable, str(PHASE1B / "verify_candidate.py"),
                 "--artifact", str(artifact_path), "--output", str(output),
                 "--expected-sha256", candidate["archive_sha256"],
                 "--source-session-id", candidate["source_session_id"],
                 "--source-artifact-id", candidate["source_artifact_id"]],
                env=env, cwd=PHASE1B, capture_output=True, text=True,
                timeout=180, check=False,
            )
            if not output.exists():
                raise RuntimeError("trusted verifier produced no result")
            record = json.loads(output.read_text())
            if result.returncode != (0 if record.get("status") == "PASS" else 1):
                raise RuntimeError("trusted verifier exit and result disagree")
            return record

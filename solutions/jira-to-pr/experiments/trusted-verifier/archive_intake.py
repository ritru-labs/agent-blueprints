"""Validate an untrusted ZIP as data and reconstruct a clean candidate tree."""

import hashlib
import json
import pathlib
import stat
import subprocess
import zipfile

from config import (
    ALLOWED_FILES,
    BASE_COMMIT,
    BASE_PREFIX,
    MAX_ARCHIVE_BYTES,
    MAX_UNCOMPRESSED_BYTES,
)


def read_candidate(archive_path, expected_sha256):
    archive_path = pathlib.Path(archive_path)
    if not archive_path.is_file() or archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("candidate archive missing or too large")
    raw = archive_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("candidate archive SHA-256 does not match the trusted record")

    accepted = {}
    total = 0
    with zipfile.ZipFile(archive_path) as bundle:
        entries = bundle.infolist()
        if len(entries) != len(ALLOWED_FILES):
            raise ValueError("candidate archive has an unexpected number of entries")
        for item in entries:
            name = item.filename
            pure = pathlib.PurePosixPath(name)
            if (name.startswith("/") or "\\" in name or "\x00" in name or
                    pure.as_posix() != name or any(part in (".", "..") for part in pure.parts)):
                raise ValueError("candidate archive has an unsafe path")
            if name in accepted:
                raise ValueError("candidate archive has a duplicate path")
            if name not in ALLOWED_FILES or item.is_dir():
                raise ValueError("candidate archive contains an unexpected file")
            mode = item.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError("candidate archive contains a special file")
            total += item.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("candidate archive expands beyond limit")
            content = bundle.read(item)
            if len(content) != item.file_size:
                raise ValueError("candidate archive entry size mismatch")
            accepted[name] = content
    if set(accepted) != set(ALLOWED_FILES):
        raise ValueError("candidate archive is missing required files")
    return digest, accepted


def baseline_files(repo_root):
    """Read only the pinned Git object's exact allowed paths, never working-tree files."""
    result = {}
    for name in ALLOWED_FILES:
        object_name = f"{BASE_COMMIT}:{BASE_PREFIX}{name}"
        process = subprocess.run(
            ["git", "show", object_name], cwd=repo_root, capture_output=True, check=False,
        )
        if process.returncode:
            raise RuntimeError(f"pinned baseline lacks {name}")
        result[name] = process.stdout
    return result


def materialize_candidate(destination, baseline, accepted):
    """Create a fresh allowlisted tree from the pinned base plus candidate files."""
    destination = pathlib.Path(destination)
    if set(baseline) != set(ALLOWED_FILES) or set(accepted) != set(ALLOWED_FILES):
        raise ValueError("candidate or baseline file set does not match the allowlist")
    if accepted["sample/app.py"] == baseline["sample/app.py"]:
        raise ValueError("candidate source is unchanged")
    if accepted["sample/tests/test_app.py"] == baseline["sample/tests/test_app.py"]:
        raise ValueError("candidate test is unchanged")
    for name in ALLOWED_FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(baseline[name])
        target.write_bytes(accepted[name])
        target.chmod(0o644)
    destination.chmod(0o755)
    for directory in (destination / "sample", destination / "sample/tests"):
        directory.chmod(0o755)
    manifest = {name: hashlib.sha256(accepted[name]).hexdigest() for name in ALLOWED_FILES}
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    tree_hash = hashlib.sha256(b"phase1b-tree-v1\0" + canonical).hexdigest()
    return tree_hash, manifest

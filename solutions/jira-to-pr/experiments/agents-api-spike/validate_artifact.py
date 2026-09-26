"""Inspect a downloaded ZIP as data; never execute the candidate code."""

import hashlib
import json
import pathlib
import stat
import sys
import zipfile

EXPECTED = {
    "sample/__init__.py",
    "sample/app.py",
    "sample/tests/test_app.py",
}
MAX_ARCHIVE_BYTES = 1_000_000
MAX_UNCOMPRESSED_BYTES = 2_000_000


def validate(path, original_app, original_test):
    archive = pathlib.Path(path)
    if not archive.is_file() or archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("archive is missing or too large")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > 25:
            raise ValueError("too many archive entries")
        names = set()
        size = 0
        for member in members:
            name = member.filename
            parts = pathlib.PurePosixPath(name).parts
            if (not name or name.startswith("/") or "\\" in name or "\x00" in name
                    or any(part in (".", "..") for part in parts)):
                raise ValueError(f"unsafe archive path: {name!r}")
            if name in names:
                raise ValueError(f"duplicate archive path: {name!r}")
            names.add(name)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"archive symlink: {name!r}")
            if member.is_dir():
                continue
            if name not in EXPECTED:
                raise ValueError(f"unexpected archive file: {name!r}")
            size += member.file_size
            if size > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("archive expands beyond limit")
        if not EXPECTED.issubset(names):
            raise ValueError("archive is missing expected files")
        if bundle.testzip() is not None:
            raise ValueError("archive CRC validation failed")
        app = bundle.read("sample/app.py")
        test = bundle.read("sample/tests/test_app.py")
    if app == pathlib.Path(original_app).read_bytes():
        raise ValueError("sample/app.py was not modified")
    if test == pathlib.Path(original_test).read_bytes():
        raise ValueError("sample/tests/test_app.py was not modified")
    if b"def add(" not in app or b"add(" not in test:
        raise ValueError("expected add function or test was not found")
    return {"sha256": digest, "bytes": archive.stat().st_size, "files": sorted(EXPECTED)}


if __name__ == "__main__":
    try:
        print(json.dumps(validate(*sys.argv[1:])))
    except (IndexError, OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"artifact validation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None

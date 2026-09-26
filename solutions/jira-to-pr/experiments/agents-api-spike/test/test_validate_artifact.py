import hashlib
import os
import pathlib
import stat
import tempfile
import unittest
import warnings
import zipfile

from validate_artifact import MAX_ARCHIVE_BYTES, validate


class ArtifactValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.original_app = self.root / "app.py"
        self.original_test = self.root / "test_app.py"
        self.original_app.write_text("def identity(x): return x\n")
        self.original_test.write_text("def test_identity(): pass\n")
        self.archive = self.root / "candidate.zip"

    def make_zip(self, extra=None):
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr("sample/__init__.py", "")
            bundle.writestr("sample/app.py", "def add(a, b): return a + b\n")
            bundle.writestr("sample/tests/test_app.py", "def test_add(): assert add(1, 2) == 3\n")
            if extra:
                bundle.writestr(*extra)

    def test_valid_archive_hash_and_files(self):
        self.make_zip()
        result = validate(self.archive, self.original_app, self.original_test)
        self.assertEqual(result["sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertEqual(len(result["files"]), 3)

    def test_rejects_traversal(self):
        self.make_zip(("../escape.py", "bad"))
        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            validate(self.archive, self.original_app, self.original_test)

    def test_rejects_unexpected_file(self):
        self.make_zip(("sample/secret.txt", "bad"))
        with self.assertRaisesRegex(ValueError, "unexpected archive file"):
            validate(self.archive, self.original_app, self.original_test)

    def test_rejects_symlink(self):
        self.make_zip()
        link = zipfile.ZipInfo("sample/link.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.archive, "a") as bundle:
            bundle.writestr(link, "app.py")
        with self.assertRaisesRegex(ValueError, "archive symlink"):
            validate(self.archive, self.original_app, self.original_test)

    def test_rejects_duplicate_path(self):
        self.make_zip()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.archive, "a") as bundle:
                bundle.writestr("sample/app.py", "duplicate")
        with self.assertRaisesRegex(ValueError, "duplicate archive path"):
            validate(self.archive, self.original_app, self.original_test)

    def test_rejects_oversized_archive(self):
        self.archive.write_bytes(os.urandom(MAX_ARCHIVE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "too large"):
            validate(self.archive, self.original_app, self.original_test)

    def test_rejects_excessive_uncompressed_size(self):
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("sample/__init__.py", "")
            bundle.writestr("sample/app.py", "def add(a, b): return a + b\n")
            bundle.writestr("sample/tests/test_app.py", "x" * 2_000_001)
        with self.assertRaisesRegex(ValueError, "expands beyond limit"):
            validate(self.archive, self.original_app, self.original_test)

    def test_rejects_missing_expected_file(self):
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr("sample/__init__.py", "")
            bundle.writestr("sample/app.py", "def add(a, b): return a + b\n")
        with self.assertRaisesRegex(ValueError, "missing expected files"):
            validate(self.archive, self.original_app, self.original_test)

    def test_rejects_backslash_path(self):
        self.make_zip(("sample\\escape.py", "bad"))
        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            validate(self.archive, self.original_app, self.original_test)

    def test_rejects_unchanged_source_or_test(self):
        for unchanged in ("source", "test"):
            with self.subTest(unchanged=unchanged):
                with zipfile.ZipFile(self.archive, "w") as bundle:
                    bundle.writestr("sample/__init__.py", "")
                    bundle.writestr("sample/app.py", self.original_app.read_bytes() if unchanged == "source" else b"def add(a, b): return a + b\n")
                    bundle.writestr("sample/tests/test_app.py", self.original_test.read_bytes() if unchanged == "test" else b"def test_add(): assert add(1, 2) == 3\n")
                with self.assertRaisesRegex(ValueError, "was not modified"):
                    validate(self.archive, self.original_app, self.original_test)


if __name__ == "__main__":
    unittest.main()

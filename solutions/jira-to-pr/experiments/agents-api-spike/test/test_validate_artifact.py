import hashlib
import pathlib
import tempfile
import unittest
import zipfile

from validate_artifact import validate


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


if __name__ == "__main__":
    unittest.main()

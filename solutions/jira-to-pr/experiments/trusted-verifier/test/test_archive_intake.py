import hashlib
import pathlib
import stat
import tempfile
import unittest
import zipfile

from archive_intake import materialize_candidate, read_candidate
from config import ALLOWED_FILES


class ArchiveIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.archive = self.root / "candidate.zip"
        self.files = {
            "sample/__init__.py": b"",
            "sample/app.py": b"def add(a, b): return a + b\n",
            "sample/tests/test_app.py": b"from sample.app import add\nassert add(1, 2) == 3\n",
        }

    def make_zip(self, extra=None):
        with zipfile.ZipFile(self.archive, "w") as bundle:
            for name, content in self.files.items():
                bundle.writestr(name, content)
            if extra:
                bundle.writestr(*extra)
        return hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_accepts_exact_files_and_builds_clean_tree(self):
        digest = self.make_zip()
        observed, accepted = read_candidate(self.archive, digest)
        baseline = dict(self.files)
        baseline["sample/app.py"] = b"def identity(x): return x\n"
        baseline["sample/tests/test_app.py"] = b"def test_identity(): pass\n"
        tree_hash, manifest = materialize_candidate(self.root / "clean", baseline, accepted)
        self.assertEqual(observed, digest)
        self.assertEqual(set(manifest), set(ALLOWED_FILES))
        self.assertEqual(len(tree_hash), 64)
        self.assertEqual((self.root / "clean/sample/app.py").read_bytes(), self.files["sample/app.py"])
        self.assertEqual(sorted(str(p.relative_to(self.root / "clean")) for p in (self.root / "clean").rglob("*.py")), sorted(ALLOWED_FILES))

    def test_rejects_hash_mismatch_before_unpacking(self):
        self.make_zip()
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            read_candidate(self.archive, "0" * 64)

    def test_rejects_traversal_even_with_matching_hash(self):
        digest = self.make_zip(("../escape.py", "bad"))
        with self.assertRaises(ValueError):
            read_candidate(self.archive, digest)

    def test_rejects_special_file_even_with_matching_hash(self):
        with zipfile.ZipFile(self.archive, "w") as bundle:
            for name, content in self.files.items():
                if name == "sample/app.py":
                    link = zipfile.ZipInfo(name)
                    link.create_system = 3
                    link.external_attr = (stat.S_IFLNK | 0o777) << 16
                    bundle.writestr(link, "somewhere")
                else:
                    bundle.writestr(name, content)
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "special file"):
            read_candidate(self.archive, digest)

    def test_rejects_unchanged_source(self):
        baseline = dict(self.files)
        baseline["sample/tests/test_app.py"] = b"different test\n"
        with self.assertRaisesRegex(ValueError, "source is unchanged"):
            materialize_candidate(self.root / "clean", baseline, self.files)


if __name__ == "__main__":
    unittest.main()

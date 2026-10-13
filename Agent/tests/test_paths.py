import os
import tempfile
import unittest
from pathlib import Path

from ytagent.paths import PathError, is_secret_path, resolve_in_roots, safe_join


class SafeJoinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        (self.root / "a.txt").write_text("x")
        self.addCleanup(self.tmp.cleanup)

    def test_joins_inside_root(self):
        self.assertEqual(safe_join(self.root, "a.txt"), (self.root / "a.txt").resolve())
        self.assertEqual(safe_join(self.root, "sub\\b.txt"), (self.root / "sub" / "b.txt").resolve())

    def test_rejects_traversal_absolute_empty(self):
        for bad in ("../x", "a/../../x", "..\\x", "/etc/passwd", "C:\\Windows", "", "  ", "a\x00b"):
            with self.subTest(bad=bad), self.assertRaises(PathError):
                safe_join(self.root, bad)

    def test_rejects_symlink_escape(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        link = self.root / "link"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Không tạo được symlink trên máy này")
        with self.assertRaises(PathError):
            safe_join(self.root, "link/secret.txt")

    def test_resolve_in_roots(self):
        self.assertEqual(resolve_in_roots([self.root], "0/a.txt"), (self.root / "a.txt").resolve())
        for bad in ("1/a.txt", "x/a.txt", "0", "0/../a"):
            with self.subTest(bad=bad), self.assertRaises(PathError):
                resolve_in_roots([self.root], bad)

    def test_secret_paths(self):
        self.assertTrue(is_secret_path("secrets/token.json"))
        self.assertTrue(is_secret_path("x/client_secret.json"))
        self.assertFalse(is_secret_path("jobs/a/frames/f1.jpg"))


if __name__ == "__main__":
    unittest.main()

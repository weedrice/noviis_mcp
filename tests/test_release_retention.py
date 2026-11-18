import os
from pathlib import Path
import tempfile
import unittest

from deploy.deploy_release import prune_releases, release_root


class ReleaseRetentionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = Path(temporary.name).resolve() / "app"
        self.root = self.app / "releases"
        self.root.mkdir(parents=True)
        self.releases = []
        for i in range(6):
            path = self.root / f"20261117T010000-{i:012x}"
            path.mkdir()
            (path / "main.py").write_text("# synthetic release\n", encoding="utf-8")
            os.utime(path, (1000 + i, 1000 + i))
            self.releases.append(path)

    def test_protects_running_and_rollback_releases_even_when_oldest(self):
        unknown = self.root / "manual-backup"
        unknown.mkdir()
        shared = self.app / ".env"
        shared.write_text("# synthetic settings", encoding="utf-8")
        prune_releases(self.app, protected=tuple(self.releases[:2]), keep=3)
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {self.releases[0].name, self.releases[1].name, self.releases[-1].name, unknown.name})
        self.assertEqual(shared.read_text(encoding="utf-8"), "# synthetic settings")

    def test_invalid_keep_count_never_removes_releases(self):
        for value in (0, 1, True, 2.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prune_releases(self.app, protected=(), keep=value)
        self.assertEqual(len(list(self.root.iterdir())), 6)

    @unittest.skipIf(os.name == "nt", "POSIX production symlinks")
    def test_current_and_external_symlink_targets_are_preserved(self):
        (self.app / "current").symlink_to(self.releases[0], target_is_directory=True)
        outside = self.app.parent / "external"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        (self.root / "20261117T010000-ffffffffffff").symlink_to(outside, target_is_directory=True)
        (self.releases[2] / "logs").symlink_to(outside, target_is_directory=True)
        prune_releases(self.app, protected=(self.releases[1],), keep=2)
        self.assertTrue(self.releases[0].is_dir())
        self.assertTrue(self.releases[1].is_dir())
        self.assertFalse(self.releases[2].exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    @unittest.skipIf(os.name == "nt", "POSIX production symlinks")
    def test_redirected_release_root_is_rejected(self):
        other_app = self.app.parent / "other-app"
        other_app.mkdir()
        (other_app / "releases").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            release_root(other_app)
        self.assertEqual(len(list(self.root.iterdir())), 6)

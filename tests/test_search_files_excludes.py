"""Directory exclusions in search_files must actually fire.

DEFAULT_EXCLUDES carries two shapes of directory entry: a glob NAME (".venv*/")
and a relative PATH ("state/debug/"). Pruning used exact set membership against
os.walk's bare directory names, so neither shape ever matched and both
exclusions were dead while looking configured. These tests pin the matcher.
Also: _setup_logger must not turn a non-string log_dir (a test double, a
mis-typed config) into a real directory on disk.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import search_files  # noqa: E402


def _dir_excluded(name, rel_parent):
    return search_files._dir_excluded(name, rel_parent)


def _tree(d):
    for rel in (".venv-py311/a.txt", "state/debug/b.txt", "debug/c.txt", "src/d.txt", "temp/e.txt"):
        p = Path(d, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("HIT\n")


class TestDirExcludedMatcher(unittest.TestCase):
    def test_glob_name_matches_venv_variants(self):
        self.assertTrue(_dir_excluded(".venv", ""))
        self.assertTrue(_dir_excluded(".venv-py311", "src"))

    def test_path_pattern_is_anchored_to_the_relative_path(self):
        self.assertTrue(_dir_excluded("debug", "state"))
        self.assertFalse(_dir_excluded("debug", ""))
        self.assertFalse(_dir_excluded("debug", "src"))

    def test_plain_names_still_match(self):
        self.assertTrue(_dir_excluded("temp", ""))
        self.assertTrue(_dir_excluded("node_modules", "a/b"))
        self.assertFalse(_dir_excluded("src", ""))


class TestSearchFilesPrunesConfiguredDirs(unittest.TestCase):
    def test_hidden_walk_prunes_venv_glob_and_state_debug_path(self):
        with tempfile.TemporaryDirectory() as d:
            _tree(d)
            out = search_files.fn("HIT", path=d, include_hidden=True)
            self.assertIn("src/d.txt", out.replace(os.sep, "/"))
            self.assertIn("debug/c.txt", out.replace(os.sep, "/"))
            self.assertNotIn(".venv-py311", out)
            self.assertNotIn("state/debug", out.replace(os.sep, "/"))
            self.assertNotIn("temp/e.txt", out.replace(os.sep, "/"))

    def test_include_temp_restores_every_pruned_dir(self):
        with tempfile.TemporaryDirectory() as d:
            _tree(d)
            out = search_files.fn("HIT", path=d, include_hidden=True, include_temp=True).replace(os.sep, "/")
            for rel in (".venv-py311/a.txt", "state/debug/b.txt", "temp/e.txt"):
                self.assertIn(rel, out)


class TestSetupLoggerRejectsNonStringLogDir(unittest.TestCase):
    def test_mock_config_creates_no_directory_in_cwd(self):
        import agent
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as hist:
            old = os.getcwd()
            os.chdir(cwd)
            try:
                with patch("agent._config", MagicMock()), patch("agent._HISTORY_DIR", hist):
                    try:
                        agent._setup_logger()
                    except Exception:  # noqa: BLE001 — only the side effect is under test
                        pass
                self.assertEqual(os.listdir(cwd), [], "a truthy non-string log_dir must not become a directory")
            finally:
                os.chdir(old)

    def test_string_log_dir_still_honoured(self):
        import agent
        with tempfile.TemporaryDirectory() as cwd:
            old = os.getcwd()
            os.chdir(cwd)
            try:
                with patch.dict(agent._config, {"log_dir": "mylogs"}):
                    agent._setup_logger()
                self.assertTrue(os.path.isdir(os.path.join(cwd, "mylogs")))
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()

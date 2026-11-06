"""## Ada 06-Nov-2026 the page must be able to say which build it is.

Matt: "add commit# very small to the bottom of the Van Page". The page is served from a git
working tree, so the answer is read from that tree at request time rather than baked into the
HTML: a hard-coded SHA is stale the moment the commit that writes it lands, which is exactly the
commit you would want to see.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
_spec = importlib.util.spec_from_file_location(
    "serve_data", os.path.join(ROOT, "bin", "serve-data.py"))
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)


class TestPageVersion(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        git(self.repo, "init", "-q")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "one")
        self.head = git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def test_reports_the_short_sha_and_branch(self):
        v = sd.page_version(repo=self.repo, max_age=0)
        self.assertEqual(self.head[:7], v["commit"])
        self.assertEqual(self.head, v["full"])
        self.assertIn(v["branch"], ("master", "main"))

    def test_a_directory_that_is_not_a_repo_reports_an_error_not_a_crash(self):
        v = sd.page_version(repo=tempfile.mkdtemp(), max_age=0)
        self.assertIn("error", v)
        self.assertNotIn("commit", v)

    def test_the_answer_is_cached_briefly(self):
        first = sd.page_version(repo=self.repo, max_age=60)
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "two")
        second = sd.page_version(repo=self.repo, max_age=60)
        self.assertEqual(first["commit"], second["commit"])   # still the cached one
        fresh = sd.page_version(repo=self.repo, max_age=0)
        self.assertNotEqual(first["commit"], fresh["commit"])


if __name__ == "__main__":
    unittest.main()

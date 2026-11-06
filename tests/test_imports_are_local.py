"""## Ada 06-Nov-2026 the suites must test the tree they live in.

Every suite in this directory used to hardcode `/home/ada/van/bin`, which is the
*deployed* tree the live van-poll/van-data services execute. Run from a worktree or a
copy, the suites then silently test the deployed files instead of the ones being
changed: a green run that cannot fail. This suite is the gate for that.
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITE_DIR = os.path.join(ROOT, "tests")

sys.path.insert(0, os.path.join(ROOT, "bin"))
import poll  # noqa: E402


class TestSuitesImportTheirOwnTree(unittest.TestCase):
    def test_poll_module_comes_from_this_tree(self):
        self.assertTrue(
            os.path.abspath(poll.__file__).startswith(os.path.abspath(ROOT)),
            "poll was imported from %s, not from %s" % (poll.__file__, ROOT),
        )

    def test_no_suite_hardcodes_the_deployed_tree(self):
        offenders = []
        for name in sorted(os.listdir(SUITE_DIR)):
            if not name.startswith("test_") or not name.endswith(".py"):
                continue
            if name == os.path.basename(__file__):
                continue  # this gate must be able to name the string it bans
            with open(os.path.join(SUITE_DIR, name)) as fh:
                # a QUOTED live-tree path is the bug; prose may name it (this gate does)
                if re.search(r"""["'](/home/ada/van)""", fh.read()):
                    offenders.append(name)
        self.assertEqual([], offenders, "suites that bypass this tree: %s" % offenders)


if __name__ == "__main__":
    unittest.main()

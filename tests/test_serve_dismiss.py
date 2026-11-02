"""## Ada 02-Nov-2026 tests for the shared alert-dismissal store.

The store is the one piece of state the page writes, and it is shared: a
dismissal made on one device must be gone on the other. It is owned by
serve-data.py and never opened by the poller, so nothing can resurrect a
closed alert on a poll tick.

Every function takes path=None and resolves DISMISSED inside the body on
purpose: a `path=DISMISSED` default binds at import time, so a test that
patches serve_data.DISMISSED would still write to the live file.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

# The file is serve-data.py (hyphen), so `import serve_data` cannot work: load it
# by path. bin/ is on sys.path for poll.py's sake, which has no hyphen.
sys.path.insert(0, "/home/ada/van/bin")
_spec = importlib.util.spec_from_file_location(
    "serve_data", "/home/ada/van/bin/serve-data.py")
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)


class TestDismissStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "dismissed.json")

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(sd.read_dismissed(self.path), {})

    def test_corrupt_file_reads_as_empty(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(sd.read_dismissed(self.path), {})

    def test_write_then_read_round_trips(self):
        sd.write_dismissed({"a-1": 100.0}, self.path)
        self.assertEqual(sd.read_dismissed(self.path), {"a-1": 100.0})

    def test_write_leaves_no_temp_file(self):
        sd.write_dismissed({"a-1": 100.0}, self.path)
        self.assertEqual(sorted(os.listdir(self.dir)), ["dismissed.json"])

    def test_written_file_has_the_ids_key(self):
        sd.write_dismissed({"a-1": 100.0}, self.path)
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"ids": {"a-1": 100.0}})

    def test_prune_drops_entries_outside_the_window(self):
        ids = {"old": 1000.0 - sd.DISMISS_WINDOW_S - 1, "new": 1000.0 - 10}
        self.assertEqual(sd.prune_dismissed(ids, now=1000.0), {"new": 990.0})

    def test_dismiss_ids_adds_and_prunes(self):
        sd.write_dismissed({"stale": 1000.0 - sd.DISMISS_WINDOW_S - 1}, self.path)
        out = sd.dismiss_ids(["a-1", "a-2"], path=self.path, now=1000.0)
        self.assertEqual(sorted(out), ["a-1", "a-2"])
        self.assertEqual(sorted(sd.read_dismissed(self.path)), ["a-1", "a-2"])

    def test_dismiss_ids_ignores_non_strings(self):
        out = sd.dismiss_ids(["ok", None, 7, ""], path=self.path, now=1000.0)
        self.assertEqual(sorted(out), ["ok"])


if __name__ == "__main__":
    unittest.main()

"""## Ada 06-Nov-2026 workstream D: /history must be able to serve the router's numbers too.

The dish has had a history endpoint since 23-Oct; the router's numbers now land in their own table
one row per poll, and the page needs the same columnar shape to chart them. Same contract, one
`table` parameter, and the same refusal to invent data when a field is not one we store.
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
_spec = importlib.util.spec_from_file_location(
    "serve_data", os.path.join(ROOT, "bin", "serve-data.py"))
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)


def store(rows):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE router (ts REAL PRIMARY KEY, signal_dbm REAL, bitrate REAL,"
                " kmwan_active TEXT, traffic_sim2 REAL)")
    con.executemany("INSERT INTO router VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


class TestRouterHistory(unittest.TestCase):
    def setUp(self):
        now = time.time()
        self.db = store([(now - 300, -58.0, 1200.0, "wwan", 1000.0),
                         (now - 200, -61.0, 866.0, "wwan", 2000.0),
                         (now - 100, -55.0, 1200.0, "wan", 3000.0)])

    def tearDown(self):
        os.unlink(self.db)

    def test_router_fields_come_back_columnar(self):
        got, err = sd.read_history(1, ["signal_dbm", "bitrate"], table="router", db=self.db)
        self.assertIsNone(err)
        self.assertEqual(3, got["count"])
        self.assertEqual([-58.0, -61.0, -55.0], got["signal_dbm"])
        self.assertEqual([1200.0, 866.0, 1200.0], got["bitrate"])

    def test_a_text_column_is_not_forced_to_float(self):
        # kmwan_active is a state word; the dish path only ever had numbers, so this is new
        got, err = sd.read_history(1, ["kmwan_active"], table="router", db=self.db)
        self.assertIsNone(err)
        self.assertEqual(["wwan", "wwan", "wan"], got["kmwan_active"])

    def test_an_unknown_table_is_refused(self):
        got, err = sd.read_history(1, ["signal_dbm"], table="nope", db=self.db)
        self.assertIsNone(got)
        self.assertIn("table", err.lower())

    def test_a_field_the_router_does_not_store_is_refused(self):
        got, err = sd.read_history(1, ["ping_drop"], table="router", db=self.db)
        self.assertIsNone(got)
        self.assertIn("field", err.lower())

    def test_the_dish_table_is_still_the_default(self):
        got, err = sd.read_history(1, ["ping_drop"], db=self.db)
        self.assertIsNone(got)          # this store has no dish table at all
        self.assertIn("dish", err.lower())


if __name__ == "__main__":
    unittest.main()

"""## Ada 06-Nov-2026 every dish row must say WHICH terminal it came from.

Two Starlink terminals share one tile (see the plan, §9/F): the tcp path to
192.168.100.1 is decided by the route, so consecutive rows can come from different kits and the
page's usage figure silently mixes them. `sw_version` alone is not an identity - it changes on a
firmware update, which is exactly what happened on 05-Nov. Store the device_info identity with
every row, and give the migration a name so "adds columns, never renames" is testable.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import poll  # noqa: E402

OLD_SCHEMA = """CREATE TABLE dish (
    ts REAL PRIMARY KEY, state TEXT, sw_version TEXT, uptime_s REAL,
    usage_down INTEGER, usage_up INTEGER);"""


class TestDishTerminal(unittest.TestCase):
    def test_reads_the_device_info_identity(self):
        self.assertEqual(
            ("ut01188c8b-8281880f-1ac2a7ee", "rev4_pez_proto1"),
            poll.dish_terminal({"id": "ut01188c8b-8281880f-1ac2a7ee",
                                "hardware_version": "rev4_pez_proto1"}))

    def test_absent_identity_is_none_not_empty(self):
        self.assertEqual((None, None), poll.dish_terminal({}))

    def test_identity_columns_are_in_the_migration_set(self):
        names = [n for n, _ in poll.ADD_COLUMNS]
        self.assertIn("dish_id", names)
        self.assertIn("dish_hw", names)
        self.assertNotIn("sw_version", names)  # never renamed, only added to


class TestEnsureColumns(unittest.TestCase):
    def test_adds_the_identity_columns_to_an_existing_table(self):
        path = os.path.join(tempfile.mkdtemp(), "van.db")
        con = sqlite3.connect(path)
        con.executescript(OLD_SCHEMA)
        con.execute("INSERT INTO dish (ts, state, sw_version, uptime_s,"
                    " usage_down, usage_up) VALUES (1.0,'CONNECTED','sw',10,1,2)")
        con.commit()
        poll.ensure_columns(con)
        cols = [r[1] for r in con.execute("PRAGMA table_info(dish)")]
        for name in ("dish_id", "dish_hw"):
            self.assertIn(name, cols)
        row = con.execute("SELECT ts, state, usage_down FROM dish").fetchall()
        self.assertEqual([(1.0, "CONNECTED", 1)], row)
        con.close()

    def test_second_run_is_a_no_op(self):
        path = os.path.join(tempfile.mkdtemp(), "van.db")
        con = sqlite3.connect(path)
        con.executescript(OLD_SCHEMA)
        poll.ensure_columns(con)
        poll.ensure_columns(con)   # must not raise
        con.close()


if __name__ == "__main__":
    unittest.main()

"""## Ada 06-Nov-2026 usage must be summed per terminal, never across a switch.

Two Starlink terminals share the route to 192.168.100.1, and the dish tile follows whichever
wins. Their counters have unrelated baselines (~47 GB and ~4 GB over this cycle), so summing
positive deltas across a switch counts the *difference between two terminals* as traffic - the
06-Nov measurement put that phantom at 0.78 GB of a 53.2 GB total, and it recurs on every
switch. Sum inside each terminal instead. The identity is `dish_id` once the poller has it
(06-Nov onward); before that, `sw_version` is the only marker that exists.
"""
import datetime as dt
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
_spec = importlib.util.spec_from_file_location(
    "serve_data", os.path.join(ROOT, "bin", "serve-data.py"))
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)

UTC = dt.timezone.utc
BASE = dt.datetime(2026, 10, 25, 1, tzinfo=UTC).timestamp()   # inside the current period


class TestTerminalGroup(unittest.TestCase):
    def test_prefers_the_stored_device_id(self):
        self.assertEqual("ut01abc", sd.terminal_group("2026.10.12.mr88921.3", "ut01abc"))

    def test_falls_back_to_the_firmware_string_before_ids_were_stored(self):
        self.assertEqual("2026.10.12.cr88921.3",
                         sd.terminal_group("2026.10.12.cr88921.3", None))
        self.assertEqual("2026.10.12.mr88921.3",
                         sd.terminal_group("2026.10.12.mr88921.3", None))

    def test_unknown_is_none(self):
        self.assertIsNone(sd.terminal_group(None, None))


class TestLegacyStore(unittest.TestCase):
    """The data server must not depend on the poller having migrated the table yet."""

    def test_older_store_without_identity_columns_still_totals(self):
        path = os.path.join(tempfile.mkdtemp(), "van.db")
        con = sqlite3.connect(path)
        con.execute("create table dish (ts real primary key, usage_down integer,"
                    " usage_up integer, sw_version text)")
        con.executemany("insert into dish values (?,?,?,?)",
                        [(BASE, 1000, 100, "sw.A"), (BASE + 80, 3000, 400, "sw.A"),
                         (BASE + 160, 900000, 5000, "sw.B"), (BASE + 240, 902000, 5500, "sw.B")])
        con.commit()
        con.close()
        got, err = sd.read_usage(db=path)
        self.assertIsNone(err)
        self.assertAlmostEqual(4000.0, got["down"], delta=1)
        self.assertEqual(2, len(got["terminals"]))


class TestPerTerminalSums(unittest.TestCase):
    """A switch between terminals must contribute nothing to the total."""

    def _db(self, rows):
        path = os.path.join(tempfile.mkdtemp(), "van.db")
        con = sqlite3.connect(path)
        con.execute("create table dish (ts real primary key, usage_down integer, usage_up integer,"
                    " sw_version text, dish_id text, dish_hw text)")
        con.executemany("insert into dish values (?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        return path

    def test_switch_between_terminals_is_not_traffic(self):
        # A: 1000 -> 3000 (real +2000). Then the tile flips to B, whose counter sits at 900000:
        # today that jump is counted as 897000 of traffic. Then B grows 900000 -> 902000 (+2000).
        rows = [(BASE, 1000, 100, "sw.A", None, None),
                (BASE + 80, 3000, 400, "sw.A", None, None),
                (BASE + 160, 900000, 5000, "sw.B", None, None),
                (BASE + 240, 902000, 5500, "sw.B", None, None)]
        got, err = sd.read_usage(db=self._db(rows))
        self.assertIsNone(err)
        self.assertAlmostEqual(4000.0, got["down"], delta=1)     # 2000 + 2000, no 897000
        self.assertAlmostEqual(800.0, got["up"], delta=1)        # 100->400 (+300) and 5000->5500 (+500)
        self.assertEqual(2, len(got["terminals"]))
        by_sw = {t["label"]: t for t in got["terminals"]}
        self.assertAlmostEqual(2000.0, by_sw["sw.A"]["down"], delta=1)
        self.assertAlmostEqual(2000.0, by_sw["sw.B"]["down"], delta=1)

    def test_grouping_uses_dish_id_once_present(self):
        # same device across two firmware builds: one group, and the step between builds counts
        rows = [(BASE, 1000, 0, "2026.10.12.mr88921.3", "ut01abc", "rev4_pez_proto1"),
                (BASE + 80, 3000, 0, "2026.10.12.mr88921.3", "ut01abc", "rev4_pez_proto1"),
                (BASE + 160, 4000, 0, "2026.11.01.mr90000.1", "ut01abc", "rev4_pez_proto1")]
        got, err = sd.read_usage(db=self._db(rows))
        self.assertIsNone(err)
        self.assertAlmostEqual(3000.0, got["down"], delta=1)
        self.assertEqual(1, len(got["terminals"]), got["terminals"])
        self.assertEqual("ut01abc", got["terminals"][0]["id"])


if __name__ == "__main__":
    unittest.main()

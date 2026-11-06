"""## Ada 06-Nov-2026 the Oct 21-24 roaming lump must not land in every period.

`read_usage()` added a hard-coded 15.8 GB lump (Oct 21-24 roaming, the four days the
database has no samples for) to whatever period it happened to be computing. The Starlink
period rolls 25th -> 24th, so from 25-Oct onward that lump is the *previous* cycle's usage
being counted into this one: the page read 68.5 GB against a 51 GB account figure, and the
15.8 GB lump is the whole gap. Gate the window here, and gate the totals here too.
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
MANUAL_TOTAL = 15.8e9


def ts(y, m, d, h=0):
    return dt.datetime(y, m, d, h, tzinfo=UTC).timestamp()


class TestManualLumpWindow(unittest.TestCase):
    def test_current_period_gets_no_lump(self):
        # 25-Oct -> 24-Nov: the window is the previous cycle, so nothing is added
        self.assertEqual((0.0, 0.0), sd.manual_lump_for(ts(2026, 10, 25), ts(2026, 11, 24, 23)))

    def test_previous_period_gets_the_lump(self):
        # 25-Sep -> 24-Oct: the window sits inside this period, so it is added, in the
        # same down/up split the live code used before this change
        down, up = sd.manual_lump_for(ts(2026, 9, 25), ts(2026, 10, 24, 23))
        self.assertAlmostEqual(MANUAL_TOTAL * (3.541 / 5.494), down, delta=1e4)
        self.assertAlmostEqual(MANUAL_TOTAL * (1.953 / 5.494), up, delta=1e4)
        self.assertAlmostEqual(MANUAL_TOTAL, down + up, delta=1e4)

    def test_period_before_the_window_gets_nothing(self):
        self.assertEqual((0.0, 0.0), sd.manual_lump_for(ts(2026, 9, 1), ts(2026, 9, 25)))

    def test_a_later_october_gets_nothing_either(self):
        # The window is a fixed 2026 date range, not "late October": the cycle that
        # contains 21-24 Oct 2027 is 25-Sep-2027 -> 24-Oct-2027 and must not pick it up.
        self.assertEqual((0.0, 0.0), sd.manual_lump_for(ts(2027, 9, 25), ts(2027, 10, 24, 23)))
        self.assertEqual((0.0, 0.0), sd.manual_lump_for(ts(2030, 1, 1), ts(2030, 1, 25)))


class TestReadUsage(unittest.TestCase):
    """read_usage must sum this period's rows for real, and take its db by argument."""

    def _db(self, rows):
        path = os.path.join(tempfile.mkdtemp(), "van.db")
        con = sqlite3.connect(path)
        con.execute("create table dish (ts real primary key, usage_down integer,"
                    " usage_up integer, sw_version text, dish_id text, dish_hw text)")
        con.executemany("insert into dish values (?,?,?,?,?,?)",
                        [(t, d, u, "sw", None, None) for t, d, u in rows])
        con.commit()
        con.close()
        return path

    def test_current_period_sums_rows_and_adds_no_lump(self):
        base = ts(2026, 10, 25) + 3600          # inside the current period, after 25-Oct
        db = self._db([(base, 1000, 100), (base + 80, 3000, 400),
                       (base + 160, 2500, 450), (base + 240, 5000, 700)])
        # Ada 06-Nov-2026 attribution is explicit now: this fixture's rows are one terminal,
        # named here so the test is about summing rather than about whose data it is (see
        # test_serve_usage_van_only.py for the van-only contract).
        got, err = sd.read_usage(db=db, van_ids=("sw",))
        self.assertIsNone(err)
        # rolling counter: sum only the increases -> down 2000+2500, up 300+50+250
        self.assertAlmostEqual(4500.0, got["down"], delta=1)
        self.assertAlmostEqual(600.0, got["up"], delta=1)
        self.assertAlmostEqual(5100.0, got["total"], delta=1)
        self.assertFalse(got["manual_lump"]["applied"])
        self.assertEqual(4, got["samples"])


if __name__ == "__main__":
    unittest.main()

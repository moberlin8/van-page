"""## Ada 06-Nov-2026 /van may only count the van's own dish, or nothing.

Matt: "ignore data from BigBoyDish, and do not show BigBoyDish data on the Van Page."

Two Starlink terminals answer the same address, and the tile follows whichever wins the route:
this box spent the whole cycle reading Geddy's kit (BigBoyDish) over the home path while the
van's own dish (EvilDish) sat powered off on the WAN jack. Their counters have unrelated
baselines, so a single total across both is not the van's usage - it is mostly someone else's.
Until EvilDish is up and its identity is confirmed, /usage can attribute nothing, and reporting
nothing is the honest answer rather than a number nobody can defend.
"""
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

VAN = "ut01188c8b-van"
OTHER = "ut01188c8b-other"


def base_ts():
    """A moment inside the CURRENT billing period: read_usage only looks there."""
    start, _ = sd.billing_period()
    return start + 3600


def store(rows):
    """Build a dish table. rows: (ts, down, up, sw_version, dish_id)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE dish (ts REAL, usage_down REAL, usage_up REAL,"
                " sw_version TEXT, dish_id TEXT, dish_hw TEXT)")
    con.executemany("INSERT INTO dish VALUES (?,?,?,?,?,?)",
                    [(ts, d, u, sw, did, "rev4") for ts, d, u, sw, did in rows])
    con.commit()
    con.close()
    return path


class TestVanOnlyAttribution(unittest.TestCase):
    def setUp(self):
        # two kits reporting in the same period, unrelated counter baselines
        b = base_ts()
        self.db = store([
            (b,        100.0, 10.0, "mr1", OTHER),
            (b + 60,   900.0, 30.0, "mr1", OTHER),    # other kit +800 down
            (b + 120,  100.0, 10.0, "cr1", VAN),
            (b + 180,  500.0, 40.0, "cr1", VAN),      # van kit +400 down, +30 up
        ])

    def tearDown(self):
        os.unlink(self.db)

    def test_with_no_van_identity_the_total_is_unknown_not_a_sum(self):
        data, err = sd.read_usage(db=self.db, van_ids=())
        self.assertIsNone(err)
        self.assertIsNone(data["total"])
        self.assertIsNone(data["down"])
        self.assertEqual([], data["attribution"]["van_dish_ids"])
        self.assertIn("van", data["attribution"]["reason"].lower())

    def test_a_known_van_id_counts_only_that_terminal(self):
        data, err = sd.read_usage(db=self.db, van_ids=(VAN,))
        self.assertIsNone(err)
        self.assertEqual(400.0, data["down"])
        self.assertEqual(30.0, data["up"])
        self.assertEqual(430.0, data["total"])

    def test_legacy_rows_without_a_dish_id_match_by_firmware(self):
        # the pre-06-Nov rows have dish_id NULL, so the firmware string is the only marker
        b = base_ts()
        db = store([(b,        10.0, 1.0, "cr88921.3", None),
                    (b + 60,  310.0, 3.0, "cr88921.3", None),
                    (b + 120,  10.0, 1.0, "mr88921.3", None),
                    (b + 180, 910.0, 9.0, "mr88921.3", None)])
        data, err = sd.read_usage(db=db, van_ids=("cr88921.3",))
        os.unlink(db)
        self.assertIsNone(err)
        self.assertEqual(300.0, data["down"])
        self.assertEqual(2.0, data["up"])

    def test_several_configured_ids_are_summed_and_an_absent_one_is_harmless(self):
        data, err = sd.read_usage(db=self.db, van_ids=(VAN, OTHER, "not-here"))
        self.assertIsNone(err)
        self.assertEqual(1200.0, data["down"])      # 800 other + 400 van, both listed

    def test_an_empty_store_for_the_van_kit_reports_unknown_with_a_reason(self):
        b = base_ts()
        db = store([(b,       100.0, 10.0, "mr1", OTHER),
                    (b + 60,  900.0, 30.0, "mr1", OTHER)])
        data, err = sd.read_usage(db=db, van_ids=(VAN,))
        os.unlink(db)
        self.assertIsNone(err)
        self.assertIsNone(data["total"])
        self.assertIn("van", data["attribution"]["reason"].lower())

    def test_every_terminal_is_still_listed_and_flagged(self):
        # the breakdown stays in the response as diagnostics; the page decides what to render
        data, _ = sd.read_usage(db=self.db, van_ids=(VAN,))
        flags = {t["id"]: t["van"] for t in data["terminals"]}
        self.assertEqual({VAN: True, OTHER: False}, flags)

    def test_the_sim_meter_travels_with_the_usage_response(self):
        # one fetch for the tile: the dish half and the SIM2 half come back together
        import sqlite3 as _sq
        b = base_ts()
        con = _sq.connect(self.db)
        con.execute("CREATE TABLE sim (ts REAL, slot INTEGER, cycle_start REAL,"
                    " period_bytes INTEGER, carried_bytes INTEGER, total_bytes INTEGER,"
                    " cap_bytes INTEGER)")
        con.executemany("INSERT INTO sim VALUES (?,?,?,?,?,?,?)", [
            (b, 1, b, 0, 0, 0, 5368709120),
            (b, 2, b, 1695872, 0, 1710600, 21474836480)])
        con.execute("UPDATE sim SET total_bytes = 1710600 WHERE slot = 2")
        con.commit(); con.close()
        data, _ = sd.read_usage(db=self.db, van_ids=(VAN,))
        self.assertEqual(1710600, data["sim"]["2"]["total_bytes"])
        self.assertEqual(21474836480, data["sim"]["2"]["cap_bytes"])
        self.assertEqual(0, data["sim"]["1"]["total_bytes"])

    def test_a_store_without_the_sim_table_reports_no_sim_data(self):
        data, _ = sd.read_usage(db=self.db, van_ids=(VAN,))
        self.assertEqual({}, data["sim"])

    def test_period_metadata_survives_an_unknown_total(self):
        # the tile still needs the cycle to render: days_left must not depend on usage
        data, _ = sd.read_usage(db=self.db, van_ids=())
        self.assertIn("days_left", data)
        self.assertIn("period_start", data)


if __name__ == "__main__":
    unittest.main()

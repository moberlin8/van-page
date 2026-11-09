"""## Ada 06-Nov-2026 every dish row must say WHICH terminal it came from.

Two Starlink terminals share one tile (see the plan, §9/F): the tcp path to
192.168.100.1 is decided by the route, so consecutive rows can come from different kits and the
page's usage figure silently mixes them. `sw_version` alone is not an identity - it changes on a
firmware update, which is exactly what happened on 05-Nov. Store the device_info identity with
every row, and give the migration a name so "adds columns, never renames" is testable.
"""
import json
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


BIGBOY = "ut01188c8b-8281880f-1ac2a7ee"   # the home kit (Matt, 09-Nov-2026)


class TestVanDishGate(unittest.TestCase):
    """## Ada 09-Nov-2026 the panel gate: /van shows the van's dish or nothing.

    Matt: "gate the dish panel on EvilDish's identity so the home dish can't appear there",
    and "ut01188c8b- is the big boy dish". Two kits answer 192.168.100.1, so the panel follows
    whichever wins the route, and it has been the home kit since 05-Nov. The gate is closed by
    default: a terminal nobody has confirmed as the van's is refused, because the alternative
    is showing someone else's dish.
    """

    def test_the_known_other_kit_is_refused(self):
        self.assertIs(False, poll.dish_terminal_is_van(BIGBOY))

    def test_a_known_other_kit_is_refused_even_if_someone_allowlists_it(self):
        # the invariant the two lists exist for: naming a kit as another's outranks an
        # allowlist entry, so a copy-paste cannot put the home dish back on the page
        self.assertIs(False, poll.dish_terminal_is_van(BIGBOY, van_ids=(BIGBOY,)))

    def test_an_allowlisted_terminal_is_the_vans(self):
        self.assertIs(True, poll.dish_terminal_is_van("ut-van-0001", van_ids=("ut-van-0001",)))

    def test_an_unlisted_terminal_is_undecided_while_no_allowlist_is_set(self):
        # with VAN_DISH_IDS empty, the denylist is the only rule: anything that is not a
        # known other kit passes, which is what lets the panel come back the moment EvilDish
        # answers (its id cannot be known before it does).
        self.assertIs(True, poll.dish_terminal_is_van("ut-someone-else"))

    def test_an_unlisted_terminal_is_refused_once_an_allowlist_exists(self):
        # filling VAN_DISH_IDS switches the gate to strict mode: membership, not absence
        self.assertIs(False, poll.dish_terminal_is_van(
            "ut-someone-else", van_ids=("ut-van-0001",)))

    def test_a_missing_identity_is_unknown_and_refused(self):
        self.assertIs(None, poll.dish_terminal_is_van(None))
        self.assertIs(None, poll.dish_terminal_is_van(""))

    def test_nothing_is_the_vans_until_its_id_is_captured(self):
        # EvilDish's own device_info id has never been recorded: the poller only started
        # storing ids on 06-Nov, by which time the WAN jack was already dark. Empty is the
        # honest state -- the gate then runs on the denylist alone, which is what keeps the
        # home kit off the page without needing an id we do not have.
        # The serial on the account page (KFV4M02179606WB8) is NOT usable here: the gRPC
        # deviceInfo exposes id/hardware_version/software_version and no serial at all.
        self.assertEqual((), poll.VAN_DISH_IDS)
        self.assertIn(BIGBOY, poll.OTHER_DISH_IDS)

    def test_the_gate_matches_ids_not_firmware_strings(self):
        # sw_version changes under a kit on a firmware update, so it is never an identity
        for listed in poll.OTHER_DISH_IDS:
            self.assertNotRegex(listed, r"\.(mr|cr)\d")

    def test_the_poller_stamps_the_verdict_into_the_snapshot(self):
        # a wiring check: the snapshot field is the gate's own answer, never recomputed in
        # the page from the raw id (two implementations of one policy is how they drift)
        with open(os.path.join(ROOT, "bin", "poll.py")) as fh:
            src = fh.read()
        self.assertIn('"van_terminal": dish_terminal_is_van(', src)


class TestSnapshotVerdict(unittest.TestCase):
    """## Ada 09-Nov-2026 one real poll tick, into a temp store: the field the page reads.

    The source check above proves the wiring exists; this proves what it writes, which is the
    part the page's behaviour actually depends on. poll_dish/poll_delilah are replaced, so no
    ssh, no gRPC and no network: the point is the snapshot, not the devices.
    """

    def _tick(self, dish_id):
        import tempfile
        tmp = tempfile.mkdtemp()
        status = {"id": dish_id, "hardware_version": "rev4_pez_proto1",
                  "state": "CONNECTED", "software_version": "2026.10.29.mr90217.1"}
        saved = {k: getattr(poll, k) for k in ("DATA", "VAR", "DB", "SNAPSHOT")}
        saved_dish, saved_delilah = poll.poll_dish, poll.poll_delilah

        def boom(*_a, **_kw):
            raise RuntimeError("not polled in this test")

        poll.DATA = tmp
        poll.VAR = os.path.join(tmp, "var")
        poll.DB = os.path.join(tmp, "van.db")
        poll.SNAPSHOT = os.path.join(tmp, "snapshot.json")
        poll.poll_dish = lambda: {"status": status, "general": {}, "ping_drop": {},
                                  "ping_latency": {}, "usage": {}, "power": {}}
        poll.poll_delilah = boom
        try:
            poll.main()
            with open(poll.SNAPSHOT) as fh:
                return json.load(fh)["dish"]
        finally:
            poll.poll_dish, poll.poll_delilah = saved_dish, saved_delilah
            for k, v in saved.items():
                setattr(poll, k, v)

    def test_the_home_kit_is_serialised_as_refused(self):
        self.assertIs(False, self._tick(BIGBOY)["van_terminal"])

    def test_the_vans_kit_is_serialised_as_shown(self):
        # the van's id is not known yet, so this borrows the two-list contract to prove the
        # True path serialises -- the day it is captured, only VAN_DISH_IDS changes
        saved = poll.VAN_DISH_IDS
        poll.VAN_DISH_IDS = ("ut-van-0001",)
        try:
            self.assertIs(True, self._tick("ut-van-0001")["van_terminal"])
        finally:
            poll.VAN_DISH_IDS = saved

    def test_a_dish_with_no_identity_serialises_as_unknown(self):
        self.assertIsNone(self._tick(None)["van_terminal"])

    def test_a_different_terminal_is_shown_while_no_allowlist_is_set(self):
        # the case that matters when EvilDish is finally powered: nothing has to be edited
        # for the panel to come back, because the home kit is the only id we refuse
        self.assertIs(True, self._tick("ut-evil-dish-unknown-yet")["van_terminal"])


if __name__ == "__main__":
    unittest.main()

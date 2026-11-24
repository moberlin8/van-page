"""## Ada 02-Nov-2026 tests for the poller's alert events: stable ids, 24h window.

The page shows 24h of alerts and lets them be dismissed by id, so ids must be
stable across ticks (a dismissed id that changes would resurrect the alert) and
the store must age out rather than cap at a fixed count.
"""
import sys
import unittest

# ## Ada 06-Nov-2026 resolve bin/ from this tree, not the deployed /home/ada/van:
# the hardcoded path made every run test the deployed copy (gate: test_imports_are_local.py)
import os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "bin"))
import poll


class TestRecordEvents(unittest.TestCase):
    def test_no_change_appends_nothing(self):
        self.assertEqual(poll.record_events([], {}, {}, False, False, 1000.0, dish_is_van=True), [])

    def test_ssid_change_appends_one_ided_event(self):
        ev = poll.record_events([], {"ssid": "A"}, {"ssid": "B"}, False, False, 1000.0, dish_is_van=True)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["id"], "1000-0")
        self.assertEqual(ev[0]["msg"], "WiFi SSID changed from A to B")

    def test_two_triggers_in_one_tick_get_distinct_ids(self):
        ev = poll.record_events([], {"ssid": "A", "kmwan_active": "wan"},
                                {"ssid": "B", "kmwan_active": "wwan"}, False, False, 1000.0, dish_is_van=True)
        self.assertEqual([e["id"] for e in ev], ["1000-0", "1000-1"])

    def test_legacy_event_without_id_gets_ts_id_and_keeps_it(self):
        ev = poll.record_events([{"ts": 5, "msg": "old"}], {}, {}, False, False, 1000.0, dish_is_van=True)
        self.assertEqual(ev[0]["id"], "5")
        ev2 = poll.record_events(ev, {}, {}, False, False, 1001.0, dish_is_van=True)
        self.assertEqual(ev2[0]["id"], "5")

    def test_event_older_than_window_is_dropped(self):
        old = {"ts": 1000.0 - poll.EVENT_WINDOW_S - 1, "msg": "ancient"}
        keep = {"ts": 1000.0 - 3600, "msg": "recent"}
        ev = poll.record_events([old, keep], {}, {}, False, False, 1000.0, dish_is_van=True)
        self.assertEqual([e["msg"] for e in ev], ["recent"])

    def test_store_cap_keeps_the_newest(self):
        # oldest-first, the order the store is actually written in: m0 oldest.
        n = poll.EVENT_KEEP_MAX + 10
        evs = [{"ts": 1000.0 - (n - i), "msg": "m%d" % i} for i in range(n)]
        out = poll.record_events(evs, {}, {}, False, False, 1000.0, dish_is_van=True)
        self.assertEqual(len(out), poll.EVENT_KEEP_MAX)
        self.assertEqual(out[0]["msg"], "m10")      # the 10 oldest dropped
        self.assertEqual(out[-1]["msg"], "m509")    # newest kept

    def test_dish_recovery_is_recorded(self):
        ev = poll.record_events([], {}, {}, True, False, 1000.0, dish_is_van=True)
        self.assertEqual([e["msg"] for e in ev], ["Dish data recovered"])


class TestDishEventsFollowTheIdentityGate(unittest.TestCase):
    """## Ada 09-Nov-2026 the dish feed's own health is only reported for the van's terminal.

    Matt, looking at the live page: "56 minutes 46 seconds ago: Dish data became stale / 38 minutes 5
    seconds ago: Dish data recovered / ...". Those four alerts tracked the HOME kit's flapping over
    the WiFi repeater -- Delilah was up for every one of them -- while the panel above them read
    "Dish Data Currently Unavailable", because that reading is refused. An alert claiming the dish's
    data is coming and going, above a panel saying there is none, is the page contradicting itself.
    """

    STALE = poll.DISH_EVENT_MSGS[0]
    RECOVERED = poll.DISH_EVENT_MSGS[1]

    def test_a_refused_reading_raises_no_stale_event(self):
        ev = poll.record_events([], {}, {}, False, True, 1000.0, dish_is_van=False)
        self.assertEqual([], [e["msg"] for e in ev])

    def test_a_refused_reading_raises_no_recovery_event(self):
        ev = poll.record_events([], {}, {}, True, False, 1000.0, dish_is_van=False)
        self.assertEqual([], [e["msg"] for e in ev])

    def test_an_unknown_identity_is_refused_too(self):
        # None is "the dish reported no identity", which is not the van's either
        ev = poll.record_events([], {}, {}, False, True, 1000.0, dish_is_van=None)
        self.assertEqual([], [e["msg"] for e in ev])

    def test_already_recorded_dish_events_are_withdrawn(self):
        # the four Matt is looking at: they describe another kit's feed, so they go
        prior = [{"ts": 900.0, "msg": self.STALE}, {"ts": 950.0, "msg": "WiFi SSID changed from A to B"},
                 {"ts": 960.0, "msg": self.RECOVERED}]
        ev = poll.record_events(prior, {}, {}, False, False, 1000.0, dish_is_van=False)
        self.assertEqual(["WiFi SSID changed from A to B"], [e["msg"] for e in ev])

    def test_other_events_are_untouched(self):
        prior = [{"ts": 900.0, "msg": "WAN Port up"},
                 {"ts": 910.0, "msg": self.STALE}]
        ev = poll.record_events(prior, {}, {}, False, False, 1000.0, dish_is_van=False)
        self.assertEqual(["WAN Port up"], [e["msg"] for e in ev])

    def test_the_vans_own_terminal_still_reports_normally(self):
        prior = [{"ts": 900.0, "msg": self.STALE}]
        ev = poll.record_events(prior, {}, {}, False, True, 1000.0, dish_is_van=True)
        self.assertEqual([self.STALE, self.STALE], [e["msg"] for e in ev])

    def test_the_gate_does_not_wedge_the_recorder(self):
        # refused, then the van's kit answers again: the pair must work from the new state
        poll.record_events([], {}, {}, False, True, 1000.0, dish_is_van=False)
        ev = poll.record_events([], {}, {}, True, False, 1000.0, dish_is_van=True)
        self.assertEqual([self.RECOVERED], [e["msg"] for e in ev])


if __name__ == "__main__":
    unittest.main()

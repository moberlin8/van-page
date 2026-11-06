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
        self.assertEqual(poll.record_events([], {}, {}, False, False, 1000.0), [])

    def test_ssid_change_appends_one_ided_event(self):
        ev = poll.record_events([], {"ssid": "A"}, {"ssid": "B"}, False, False, 1000.0)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["id"], "1000-0")
        self.assertEqual(ev[0]["msg"], "WiFi SSID changed from A to B")

    def test_two_triggers_in_one_tick_get_distinct_ids(self):
        ev = poll.record_events([], {"ssid": "A", "kmwan_active": "wan"},
                                {"ssid": "B", "kmwan_active": "wwan"}, False, False, 1000.0)
        self.assertEqual([e["id"] for e in ev], ["1000-0", "1000-1"])

    def test_legacy_event_without_id_gets_ts_id_and_keeps_it(self):
        ev = poll.record_events([{"ts": 5, "msg": "old"}], {}, {}, False, False, 1000.0)
        self.assertEqual(ev[0]["id"], "5")
        ev2 = poll.record_events(ev, {}, {}, False, False, 1001.0)
        self.assertEqual(ev2[0]["id"], "5")

    def test_event_older_than_window_is_dropped(self):
        old = {"ts": 1000.0 - poll.EVENT_WINDOW_S - 1, "msg": "ancient"}
        keep = {"ts": 1000.0 - 3600, "msg": "recent"}
        ev = poll.record_events([old, keep], {}, {}, False, False, 1000.0)
        self.assertEqual([e["msg"] for e in ev], ["recent"])

    def test_store_cap_keeps_the_newest(self):
        # oldest-first, the order the store is actually written in: m0 oldest.
        n = poll.EVENT_KEEP_MAX + 10
        evs = [{"ts": 1000.0 - (n - i), "msg": "m%d" % i} for i in range(n)]
        out = poll.record_events(evs, {}, {}, False, False, 1000.0)
        self.assertEqual(len(out), poll.EVENT_KEEP_MAX)
        self.assertEqual(out[0]["msg"], "m10")      # the 10 oldest dropped
        self.assertEqual(out[-1]["msg"], "m509")    # newest kept

    def test_dish_recovery_is_recorded(self):
        ev = poll.record_events([], {}, {}, True, False, 1000.0)
        self.assertEqual([e["msg"] for e in ev], ["Dish data recovered"])


if __name__ == "__main__":
    unittest.main()

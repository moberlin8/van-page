"""## Ada 06-Nov-2026 the SIM meter must survive the router's counter, not trust it.

GL's per-SIM counter (`/tmp/traffic_sim<N>`, mirrored to flash every 30 s) is the sum of the
module's AT+QGDCNT deltas, and it has already reset twice this cycle (6657 -> 9108 -> 1368 on
06-Nov, then 1.19 MB after a module re-init). The flash mirror follows the reset rather than
protecting against it. Carrying the peak forward when the counter drops costs at most one
60-second sample per reset instead of the whole cycle. SIM1 is the last-resort card with a low,
still-unknown allowance (Matt, 06-Nov), so it is measured the same way and treated separately.
"""
import os
import sys
import unittest
import datetime as dt
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import poll  # noqa: E402

EST = dt.timezone(dt.timedelta(hours=-5))


def epoch(y, m, d, h=0):
    return dt.datetime(y, m, d, h, tzinfo=EST).timestamp()


class TestSimCycleStart(unittest.TestCase):
    """The SIM2 plan renews on the 28th (portal, 06-Nov; cron `0 0 28 * *` on the router).

    Asserted in LOCAL calendar terms, not epochs: the anchor is local, so an epoch expectation
    would have to pick DST conventions and would be testing the test's own arithmetic.
    """

    def local(self, ts):
        return time.localtime(ts)[:4]          # (year, month, day, hour)

    def test_mid_cycle_returns_the_last_reset(self):
        self.assertEqual((2026, 10, 28, 0),
                         self.local(poll.sim_cycle_start(epoch(2026, 11, 6, 13), 28, 0)))

    def test_at_the_boundary_the_new_cycle_has_started(self):
        self.assertEqual((2026, 11, 28, 0),
                         self.local(poll.sim_cycle_start(epoch(2026, 11, 28, 0), 28, 0)))

    def test_after_the_boundary_returns_that_reset(self):
        self.assertEqual((2026, 11, 28, 0),
                         self.local(poll.sim_cycle_start(epoch(2026, 11, 29, 9), 28, 0)))

    def test_minute_before_the_boundary_is_still_the_old_cycle(self):
        self.assertEqual((2026, 10, 28, 0),
                         self.local(poll.sim_cycle_start(epoch(2026, 11, 27, 23), 28, 0)))

    def test_hour_anchor_is_honoured(self):
        self.assertEqual((2026, 11, 28, 6),
                         self.local(poll.sim_cycle_start(epoch(2026, 11, 28, 8), 28, 6)))

    def test_nonsense_anchor_falls_back_to_day_28(self):
        self.assertEqual((2026, 10, 28, 0),
                         self.local(poll.sim_cycle_start(epoch(2026, 11, 6, 13), "", None)))


class TestSimAccumulate(unittest.TestCase):
    def test_first_sample_seeds_the_cycle(self):
        st = poll.sim_accumulate(None, 1000, 500.0)
        self.assertEqual({"carried": 0, "last": 1000, "cycle_start": 500.0, "total": 1000}, st)

    def test_growth_extends_the_total(self):
        st = poll.sim_accumulate({"carried": 0, "last": 1000, "cycle_start": 500.0}, 1500, 500.0)
        self.assertEqual(1500, st["total"])
        self.assertEqual(1500, st["last"])

    def test_a_drop_carries_the_peak_forward(self):
        # the module re-initialised: the counter restarted from near zero
        st = poll.sim_accumulate({"carried": 0, "last": 1000, "cycle_start": 500.0}, 100, 500.0)
        self.assertEqual(1000, st["carried"])
        self.assertEqual(1100, st["total"])

    def test_growth_after_a_reset_adds_to_the_carried_peak(self):
        st = poll.sim_accumulate({"carried": 1000, "last": 100, "cycle_start": 500.0}, 400, 500.0)
        self.assertEqual(1000, st["carried"])
        self.assertEqual(1400, st["total"])

    def test_a_new_cycle_starts_from_zero(self):
        st = poll.sim_accumulate({"carried": 9_000_000, "last": 100, "cycle_start": 500.0},
                                 200, 999.0)
        self.assertEqual(0, st["carried"])
        self.assertEqual(200, st["total"])

    def test_missing_sample_keeps_the_state(self):
        st = poll.sim_accumulate({"carried": 1000, "last": 100, "cycle_start": 500.0}, None, 500.0)
        self.assertEqual(1000, st["carried"])
        self.assertEqual(1100, st["total"])


if __name__ == "__main__":
    unittest.main()

"""## Ada 06-Nov-2026 workstream D: the router's numbers, one row per poll.

The page can only show a snapshot of Delilah today; a burn rate, a failover that happened at 03:00,
or "was the signal worse before the reboot" all need history. This is the row the poller writes,
kept pure so it can be tested without a router: the probe hands back strings, and the interesting
cases are the ones that are not a single number ("18,99" for csq, "0.12 0.10 0.08" for load).
"""
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
_spec = importlib.util.spec_from_file_location("poll", os.path.join(ROOT, "bin", "poll.py"))
poll = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poll)

PROBE = {
    "signal_dbm": "-58", "noise_dbm": "-92", "bitrate": "1200", "link_quality": "70/70",
    "ssid": "Dobermn tv", "channel": "36", "wwan_uptime": "14296",
    "kmwan_active": "wwan", "kmwan_level": "3", "kmwan_active_metric": "3",
    "csq": "18,99", "qcsq": "lte,rsrp=-95,rsrq=-11,sinr=12,rssi=-67",
    "temp_c": "48", "load": "0.12 0.10 0.08", "mem_avail": "180000", "mem_total": "256000",
    "uptime": "90000", "rx_bytes": "1234567", "tx_bytes": "765432", "sim_slot": "1",
    "traffic_sim1": "413280", "traffic_sim2": "1849592", "cell_ok": "1",
    "cereg": "0,1", "cops": "310260", "apn": "fast.t-mobile.com",
}


class TestRouterRow(unittest.TestCase):
    def test_every_column_is_present_and_in_order(self):
        row = poll.router_row(1000.0, PROBE)
        self.assertEqual(("ts",) + poll.ROUTER_COLUMNS, tuple(row.keys()))
        self.assertEqual(1000.0, row["ts"])

    def test_plain_numbers_are_numbers(self):
        row = poll.router_row(1.0, PROBE)
        self.assertEqual(-58.0, row["signal_dbm"])
        self.assertEqual(1200.0, row["bitrate"])
        self.assertEqual(14296.0, row["wwan_uptime"])
        self.assertEqual(413280.0, row["traffic_sim1"])

    def test_a_comma_pair_keeps_its_first_number(self):
        # csq is "rssi,ber"; the ber is not a measurement anyone charts
        self.assertEqual(18.0, poll.router_row(1.0, PROBE)["csq"])

    def test_a_load_average_keeps_its_first_number(self):
        self.assertEqual(0.12, poll.router_row(1.0, PROBE)["load1"])

    def test_a_ratio_keeps_its_first_number(self):
        self.assertEqual(70.0, poll.router_row(1.0, PROBE)["link_quality"])

    def test_state_strings_stay_strings(self):
        row = poll.router_row(1.0, PROBE)
        self.assertEqual("wwan", row["kmwan_active"])
        self.assertEqual("Dobermn tv", row["ssid"])
        self.assertEqual("fast.t-mobile.com", row["apn"])
        self.assertIn("rsrp=-95", row["qcsq"])

    def test_missing_and_blank_values_are_null_not_zero(self):
        row = poll.router_row(1.0, {"signal_dbm": "", "bitrate": None, "ssid": ""})
        self.assertIsNone(row["signal_dbm"])
        self.assertIsNone(row["bitrate"])
        self.assertIsNone(row["ssid"])
        self.assertIsNone(row["traffic_sim2"])

    def test_nonsense_is_null_rather_than_an_exception(self):
        row = poll.router_row(1.0, {"signal_dbm": "n/a", "load": "high", "csq": "x,y"})
        self.assertIsNone(row["signal_dbm"])
        self.assertIsNone(row["load1"])
        self.assertIsNone(row["csq"])


if __name__ == "__main__":
    unittest.main()

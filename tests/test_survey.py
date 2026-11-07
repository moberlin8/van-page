"""## Ada 06-Nov-2026 the WAN survey: ranking, the switch, and filing the result (plan §15j).

The engine runs on the router and shells out to ping and curl, so what is testable here is the part
that decides: which path wins, what the switch does to kmwan, and whether a result can be filed
twice. Those are the parts where a mistake is expensive - a wrong winner moves the van onto a worse
link, and a wrong switch renumbers the failover order behind Matt's back.
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ws = load("wan_survey", os.path.join(ROOT, "bin", "wan-survey.py"))
sd = load("serve_data", os.path.join(ROOT, "bin", "serve-data.py"))


def path(pid, ok=True, speed=1000000.0, rtt=20.0, member=None):
    return {"id": pid, "label": pid, "device": pid, "member": member or pid,
            "ok": ok, "speed_bps": speed, "rtt_ms": rtt, "loss_pct": 0}


class TestRank(unittest.TestCase):
    def test_fastest_wins(self):
        r = ws.rank([path("a", speed=1e6), path("b", speed=3e6), path("c", speed=2e6)])
        self.assertEqual(["b", "c", "a"], [x["id"] for x in r])

    def test_latency_breaks_a_tie(self):
        r = ws.rank([path("a", speed=1e6, rtt=90), path("b", speed=1e6, rtt=10)])
        self.assertEqual(["b", "a"], [x["id"] for x in r])

    def test_failures_go_last_however_fast_they_look(self):
        r = ws.rank([path("a", ok=False, speed=9e6), path("b", speed=1e5)])
        self.assertEqual(["b", "a"], [x["id"] for x in r])

    def test_a_missing_latency_does_not_beat_a_measured_one(self):
        r = ws.rank([path("a", speed=1e6, rtt=None), path("b", speed=1e6, rtt=50)])
        self.assertEqual(["b", "a"], [x["id"] for x in r])


class TestSwitch(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.metrics = {"wan": 2, "wwan": 1, "modem_0001": 3}
        self._sh, self._cm = ws.sh, ws.current_metrics
        ws.sh = lambda cmd, timeout=30: (self.calls.append(cmd), "")[1]
        ws.current_metrics = lambda: dict(self.metrics)

    def tearDown(self):
        ws.sh, ws.current_metrics = self._sh, self._cm

    def test_already_first_changes_nothing(self):
        out = ws.switch("wwan", [path("wwan"), path("wan"), path("modem_0001")])
        self.assertFalse(out["switched"])
        self.assertEqual([], self.calls)

    def test_a_win_swaps_with_the_current_first(self):
        out = ws.switch("modem_0001", [path("modem_0001"), path("wwan"), path("wan")])
        self.assertTrue(out["switched"])
        self.assertEqual({"wan": 2, "wwan": 3, "modem_0001": 1}, out["after"])

    def test_the_untouched_member_keeps_its_metric(self):
        # The WAN jack is the Starlink path and must stay ahead of cellular. A survey that had
        # nothing to do with it must not renumber it.
        out = ws.switch("modem_0001", [path("modem_0001"), path("wwan"), path("wan")])
        self.assertEqual(2, out["after"]["wan"])

    def test_the_switch_is_committed_and_applied(self):
        ws.switch("modem_0001", [path("modem_0001"), path("wwan"), path("wan")])
        joined = " | ".join(self.calls)
        self.assertIn("uci set kmwan.modem_0001.metric=1", joined)
        self.assertIn("uci commit kmwan", joined)
        self.assertIn("/etc/init.d/kmwan restart", joined)

    def test_the_previous_metrics_are_reported(self):
        out = ws.switch("modem_0001", [path("modem_0001"), path("wwan"), path("wan")])
        self.assertEqual({"wan": 2, "wwan": 1, "modem_0001": 3}, out["before"])


class TestStartSurveyValidation(unittest.TestCase):
    def test_empty_name_is_rejected(self):
        self.assertEqual(400, sd.start_survey("")[0])
        self.assertEqual(400, sd.start_survey("   ")[0])
        self.assertEqual(400, sd.start_survey(None)[0])

    def test_long_name_is_rejected(self):
        self.assertEqual(400, sd.start_survey("x" * 65)[0])


class TestFiling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.places = os.path.join(self.tmp, "places.db")
        con = sd.places_con(self.places)
        con.execute("INSERT INTO place (name, first_ts, last_ts) VALUES ('Campground X', NULL, NULL)")
        con.commit()
        con.close()

    def run_doc(self, run_id="1"):
        return {"state": "done", "run_id": run_id, "name": "Campground X",
                "finished": "2026-11-07T01:30:00Z", "winner": "wwan",
                "results": [{"id": "wwan", "ok": True, "speed_bps": 3e6}]}

    def test_a_finished_run_is_filed_against_its_place(self):
        sd._file_survey(self.run_doc(), places_db=self.places)
        con = sqlite3.connect(self.places)
        row = con.execute("SELECT place_id, run_id FROM survey").fetchone()
        con.close()
        self.assertEqual("1", row[1])
        self.assertIsNotNone(row[0])

    def test_filing_the_same_run_twice_writes_one_row(self):
        sd._file_survey(self.run_doc(), places_db=self.places)
        sd._file_survey(self.run_doc(), places_db=self.places)
        con = sqlite3.connect(self.places)
        self.assertEqual(1, con.execute("SELECT COUNT(*) FROM survey").fetchone()[0])
        con.close()

    def test_a_run_with_no_name_is_not_filed(self):
        doc = self.run_doc()
        doc["name"] = ""
        sd._file_survey(doc, places_db=self.places)
        con = sqlite3.connect(self.places)
        self.assertEqual(0, con.execute("SELECT COUNT(*) FROM survey").fetchone()[0])
        con.close()


class TestSurveyState(unittest.TestCase):
    def setUp(self):
        self._delilah = sd._delilah
        sd.SURVEY_CACHE.update({"at": 0.0, "value": None})

    def tearDown(self):
        sd._delilah = self._delilah
        sd.SURVEY_CACHE.update({"at": 0.0, "value": None})

    def test_a_running_survey_is_reported_as_running(self):
        class R:
            returncode = 0
            stdout = json.dumps({"state": "measuring", "run_id": "7"})
            stderr = ""
        sd._delilah = lambda cmd, timeout=20: R()
        self.assertEqual("measuring", sd.survey_state(max_age=0)["state"])

    def test_an_unreachable_router_keeps_the_last_answer_and_marks_it_stale(self):
        class Ok:
            returncode = 0
            stdout = json.dumps({"state": "measuring", "run_id": "7"})
            stderr = ""
        sd._delilah = lambda cmd, timeout=20: Ok()
        sd.survey_state(max_age=0)

        class Bad:
            returncode = 255
            stdout = ""
            stderr = "ssh: connect to host delilah port 22: No route to host"
        sd._delilah = lambda cmd, timeout=20: Bad()
        out = sd.survey_state(max_age=0)
        self.assertEqual("measuring", out["state"])
        self.assertTrue(out["stale"])


if __name__ == "__main__":
    unittest.main()

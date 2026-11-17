"""## Ada 17-Nov-2026 tests for the usage-plan routes (GET/POST /caps).

The plan is the one place the caps, the warning levels and each service's billing
day live, and the form at /van/caps.html writes it. Three failure modes matter and
each is pinned here: a save that is validated away SILENTLY (the form would read as
broken), a save that leaves a half-written file for the page and the server to read,
and a billing day that is accepted but never reaches the usage period.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, "/home/ada/van/bin")
_spec = importlib.util.spec_from_file_location(
    "serve_data", "/home/ada/van/bin/serve-data.py")
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)


class CapsHarness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved = (sd.ROOT, sd.CAPS_FILE)
        sd.ROOT = self.dir
        sd.CAPS_FILE = os.path.join(self.dir, "caps.json")
        self.server = sd.Server(("127.0.0.1", 0), sd.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        sd.ROOT, sd.CAPS_FILE = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def req(self, method, path, body=None, raw=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = raw if raw is not None else (json.dumps(body) if body is not None else None)
        c.request(method, path, payload, {"Content-Type": "application/json"})
        r = c.getresponse()
        data, headers = r.read(), dict(r.getheaders())
        c.close()
        return r.status, headers, data


class TestCapsRoutes(CapsHarness):
    def test_get_returns_defaults_when_no_file(self):
        status, headers, body = self.req("GET", "/caps")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        d = json.loads(body)
        self.assertEqual(d["caps"], sd.CAPS_DEFAULTS)
        self.assertEqual(d["defaults"], sd.CAPS_DEFAULTS)
        # the ranges ride along so the form does not hold a second copy of the limits
        self.assertEqual(d["ranges"], json.loads(json.dumps(sd.CAPS_RANGES)))
        self.assertEqual(d["path"], sd.CAPS_FILE)

    def test_post_then_get_round_trips(self):
        status, _, body = self.req("POST", "/caps", {
            "dish": {"plan_gb": 300, "warn_gb": 150, "usd_gb": 0.27, "billing_day": 12},
            "sim2": {"plan_gb": 25, "warn_gb": 20, "bank_gb": 75, "usd_gb": 1.5,
                     "billing_day": 3},
        })
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        got = json.loads(self.req("GET", "/caps")[2])["caps"]
        self.assertEqual(got["dish"]["plan_gb"], 300.0)
        self.assertEqual(got["dish"]["billing_day"], 12)
        self.assertEqual(got["sim2"]["bank_gb"], 75.0)
        self.assertEqual(got["sim2"]["billing_day"], 3)
        # and it landed on disk, not only in the reply
        with open(sd.CAPS_FILE) as fh:
            self.assertEqual(json.load(fh)["dish"]["billing_day"], 12)

    def test_form_strings_are_accepted(self):
        # a form posts strings; each must coerce, not be rejected as "not a number"
        status, _, _ = self.req("POST", "/caps", {"dish": {"plan_gb": "150", "billing_day": "7"}})
        self.assertEqual(status, 200)
        got = json.loads(self.req("GET", "/caps")[2])["caps"]
        self.assertEqual(got["dish"]["plan_gb"], 150.0)
        self.assertEqual(got["dish"]["billing_day"], 7)

    def test_partial_post_leaves_the_other_fields_alone(self):
        self.req("POST", "/caps", {"dish": {"plan_gb": 100, "billing_day": 25},
                                   "sim2": {"bank_gb": 60}})
        status, _, _ = self.req("POST", "/caps", {"dish": {"billing_day": 10}})
        self.assertEqual(status, 200)
        got = json.loads(self.req("GET", "/caps")[2])["caps"]
        self.assertEqual(got["dish"]["billing_day"], 10)
        self.assertEqual(got["dish"]["plan_gb"], 100.0)   # untouched
        self.assertEqual(got["sim2"]["bank_gb"], 60.0)    # untouched
        self.assertEqual(got["sim2"]["billing_day"], 28)  # default

    def test_out_of_range_is_a_400_that_names_the_field(self):
        for body, field in (({"dish": {"plan_gb": -5}}, "dish.plan_gb"),
                            ({"sim2": {"billing_day": 31}}, "sim2.billing_day"),
                            ({"dish": {"billing_day": 0}}, "dish.billing_day"),
                            ({"dish": {"plan_gb": "abc"}}, "dish.plan_gb"),
                            ({"dish": {"plan_gb": True}}, "dish.plan_gb")):
            status, _, raw = self.req("POST", "/caps", body)
            self.assertEqual(status, 400, body)
            errs = json.loads(raw)["errors"]
            self.assertTrue(any(e.startswith(field) for e in errs), (body, errs))

    def test_malformed_json_is_a_400_not_a_crash(self):
        status, _, raw = self.req("POST", "/caps", raw="{not json")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(raw))

    def test_a_refusal_does_not_touch_the_file(self):
        self.req("POST", "/caps", {"dish": {"billing_day": 9}})
        with open(sd.CAPS_FILE) as fh:
            before = fh.read()
        self.req("POST", "/caps", {"dish": {"plan_gb": -1}})
        with open(sd.CAPS_FILE) as fh:
            self.assertEqual(fh.read(), before)

    def test_cors_and_post_are_advertised(self):
        _, headers, _ = self.req("GET", "/caps")
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods", ""))

    def test_write_failure_is_a_500_with_json(self):
        # van-data.service runs with ReadOnlyPaths on the poller's data and this file lives with the
        # server's own writable state, but a full or mis-mounted disk is still reachable: a save that
        # fails must say so rather than answer 200 over a file it never wrote.
        ro = tempfile.mkdtemp()
        os.chmod(ro, 0o500)
        saved = sd.CAPS_FILE
        sd.CAPS_FILE = os.path.join(ro, "caps.json")
        try:
            status, _, raw = self.req("POST", "/caps", {"dish": {"plan_gb": 100}})
            self.assertEqual(status, 500)
            self.assertIn("error", json.loads(raw))
        finally:
            sd.CAPS_FILE = saved
            os.chmod(ro, 0o700)
            shutil.rmtree(ro, ignore_errors=True)


class TestBillingPeriodFollowsThePlan(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved = sd.CAPS_FILE
        sd.CAPS_FILE = os.path.join(self.dir, "caps.json")

    def tearDown(self):
        sd.CAPS_FILE = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def _utc(y, m, d, h=0):
        import datetime as dt
        return dt.datetime(y, m, d, h, tzinfo=dt.timezone.utc).timestamp()

    def test_period_is_anchored_to_the_given_day(self):
        now = self._utc(2026, 11, 17, 8)
        start, end = sd.billing_period(now=now, day=25)
        self.assertEqual(start, self._utc(2026, 10, 25))       # 25 Oct -> 24 Nov
        self.assertEqual(end, self._utc(2026, 11, 24, 23) + 3599)
        start, end = sd.billing_period(now=now, day=1)
        self.assertEqual(start, self._utc(2026, 11, 1))
        self.assertEqual(end, self._utc(2026, 11, 30, 23) + 3599)

    def test_day_later_in_the_month_steps_back_one_month(self):
        now = self._utc(2026, 11, 17, 8)
        start, end = sd.billing_period(now=now, day=20)        # 17th < 20th
        self.assertEqual(start, self._utc(2026, 10, 20))
        self.assertEqual(end, self._utc(2026, 11, 19, 23) + 3599)

    def test_day_defaults_to_the_configured_dish_day(self):
        self.assertEqual(sd.billing_period(now=self._utc(2026, 11, 17, 8), day=25),
                         sd.billing_period(now=self._utc(2026, 11, 17, 8)))
        sd.write_caps(sd.validate_caps({"dish": {"billing_day": 5}})[0])
        self.assertEqual(sd.billing_period(now=self._utc(2026, 11, 17, 8))[0],
                         self._utc(2026, 11, 5))
        sd.write_caps(sd.validate_caps({"dish": {"billing_day": 20}})[0])
        self.assertEqual(sd.billing_period(now=self._utc(2026, 11, 17, 8))[0],
                         self._utc(2026, 10, 20))

    def test_an_out_of_range_day_on_disk_falls_back_to_the_default(self):
        # a hand-edited file must not be able to crash every /usage request, and an impossible day
        # (31) is rejected by validation rather than reaching datetime.replace()
        with open(sd.CAPS_FILE, "w") as fh:
            fh.write('{"dish": {"billing_day": 31}}')
        start, _ = sd.billing_period(now=self._utc(2026, 11, 17, 8))
        self.assertEqual(start, self._utc(2026, 10, 25))       # the default day


class TestReadCaps(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved = sd.CAPS_FILE
        sd.CAPS_FILE = os.path.join(self.dir, "caps.json")

    def tearDown(self):
        sd.CAPS_FILE = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_absent_file_is_the_defaults(self):
        self.assertEqual(sd.read_caps(), sd.CAPS_DEFAULTS)

    def test_corrupt_file_is_the_defaults_not_a_crash(self):
        with open(sd.CAPS_FILE, "w") as fh:
            fh.write("{ this is not json")
        self.assertEqual(sd.read_caps(), sd.CAPS_DEFAULTS)

    def test_a_saved_field_persists_and_a_missing_one_is_filled(self):
        with open(sd.CAPS_FILE, "w") as fh:
            json.dump({"dish": {"plan_gb": 42.5}}, fh)
        got = sd.read_caps()
        self.assertEqual(got["dish"]["plan_gb"], 42.5)
        self.assertEqual(got["dish"]["billing_day"], 25)       # default preserved
        self.assertEqual(got["sim2"], sd.CAPS_DEFAULTS["sim2"])


if __name__ == "__main__":
    unittest.main()

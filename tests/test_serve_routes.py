"""## Ada 02-Nov-2026 route tests for the shared alert-dismissal endpoints.

Two of these pin failure modes that would be invisible until a human hit them:
/dismissed must be `no-store` (inheriting snapshot.json's 60s cache would hand
back pre-dismissal state and the alert would flash back on the next 15s tick),
and /dismiss_all must dismiss only the ids it is given (otherwise an alert that
fired between render and click is swallowed unseen).
"""
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, "/home/ada/van/bin")
_spec = importlib.util.spec_from_file_location(
    "serve_data", "/home/ada/van/bin/serve-data.py")
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)


class TestDismissRoutes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # save/restore: unittest discover runs every module in one process, so a
        # leaked module global would follow the other suites around.
        self._saved = (sd.ROOT, sd.DISMISSED)
        sd.ROOT = self.dir
        sd.DISMISSED = os.path.join(self.dir, "dismissed.json")
        # a real static file, so the cache-header test exercises a 200 and not a 404
        with open(os.path.join(self.dir, "snapshot.json"), "w") as f:
            f.write("{}")
        self.server = sd.Server(("127.0.0.1", 0), sd.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        sd.ROOT, sd.DISMISSED = self._saved

    def _req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body) if body is not None else None
        c.request(method, path, payload, {"Content-Type": "application/json"})
        r = c.getresponse()
        data, headers = r.read(), dict(r.getheaders())
        c.close()
        return r.status, headers, data

    def test_get_dismissed_is_empty_and_no_store(self):
        status, headers, body = self._req("GET", "/dismissed")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(json.loads(body)["ids"], {})

    def test_snapshot_keeps_its_60s_cache(self):
        # the no-store override must not leak onto every other route
        status, headers, _ = self._req("GET", "/snapshot.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "max-age=60")

    def test_post_dismiss_persists(self):
        status, _, body = self._req("POST", "/dismiss", {"id": "1000-0"})
        self.assertEqual(status, 200)
        self.assertIn("1000-0", json.loads(body)["ids"])
        self.assertEqual(sorted(sd.read_dismissed(sd.DISMISSED)), ["1000-0"])

    def test_post_dismiss_without_id_is_400(self):
        status, _, _ = self._req("POST", "/dismiss", {})
        self.assertEqual(status, 400)

    def test_post_dismiss_with_bad_json_is_400(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/dismiss", "{not json", {"Content-Type": "application/json"})
        r = c.getresponse()
        status = r.status
        r.read()
        c.close()
        self.assertEqual(status, 400)

    def test_post_dismiss_all_only_dismisses_given_ids(self):
        self._req("POST", "/dismiss", {"id": "keep-me-not"})
        status, _, body = self._req("POST", "/dismiss_all", {"ids": ["a-1", "a-2"]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["count"], 2)
        got = sorted(sd.read_dismissed(sd.DISMISSED))
        self.assertEqual(got, ["a-1", "a-2", "keep-me-not"])

    def test_cors_allows_post(self):
        _, headers, _ = self._req("GET", "/dismissed")
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods", ""))
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")

    def test_history_still_rejects_post(self):
        status, _, _ = self._req("POST", "/history", {})
        self.assertEqual(status, 405)

    def test_dismiss_write_failure_is_500_json_not_an_empty_reply(self):
        # van-data.service runs with ReadOnlyPaths=/srv/van/data, so a store
        # pointed somewhere unwritable is a REACHABLE state, not a hypothetical.
        # It must answer with JSON that says so: an empty reply (or a silent
        # success) leaves the page looking like the alert was dismissed when it
        # was not, which is exactly the bug a human would report as "dismiss
        # does nothing".
        ro = tempfile.mkdtemp()
        os.chmod(ro, 0o500)
        saved = sd.DISMISSED
        sd.DISMISSED = os.path.join(ro, "dismissed.json")
        try:
            status, _, body = self._req("POST", "/dismiss", {"id": "x"})
            self.assertEqual(status, 500)
            self.assertIn("error", json.loads(body))
        finally:
            sd.DISMISSED = saved
            os.chmod(ro, 0o700)


if __name__ == "__main__":
    unittest.main()

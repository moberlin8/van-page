"""## Ada 09-Nov-2026 the position panel draws the whole record, not a fixed window.

Matt: "the whole trip's track rather than the last day". A track is a SHAPE, and "where have we
been" is cumulative, while the charts beside it answer "how was the link lately" and stay on their
own window. So `hours` becomes optional: falsy means everything the poller holds.

Two consequences the tests pin, both of which only appear once the window is unbounded:

  * the point cap that keeps a chart drawable now actually bites, so the payload is bounded even
    though the query is not;
  * the LAST fix must survive the thinning. The panel draws it as the amber "you are here" pin, so
    dropping it would put the pin on a fix up to `stride` intervals old -- minutes to hours of
    driving away on a trip.
"""
import http.client
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

# ## Ada 06-Nov-2026 resolve bin/ from this tree, not the deployed /home/ada/van
# (gate: test_imports_are_local.py)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
_spec = importlib.util.spec_from_file_location(
    "serve_data", os.path.join(ROOT, "bin", "serve-data.py"))
assert _spec and _spec.loader
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)


def track_db(rows):
    """A van.db-shaped store holding only the gps table: (ts ISO string, lat, lon, alt, sp, sats)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gps (ts TEXT PRIMARY KEY, lat REAL, lon REAL, alt REAL,"
                " speed REAL, sats INTEGER, hdop REAL, fix INTEGER)")
    con.executemany("INSERT INTO gps (ts, lat, lon, alt, speed, sats, fix)"
                    " VALUES (?,?,?,?,?,?,3)", rows)
    con.commit()
    con.close()
    return path


def iso(epoch):
    import datetime
    return datetime.datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestReadTrack(unittest.TestCase):
    def setUp(self):
        now = time.time()
        # three fixes inside 24 h, one well outside it
        self.rows = [(iso(now - 72 * 3600), 36.0, -78.0, 150.0, 0.0, 10),
                     (iso(now - 3 * 3600), 36.1, -78.1, 151.0, 0.0, 12),
                     (iso(now - 2 * 3600), 36.2, -78.2, 152.0, 0.0, 14),
                     (iso(now - 1 * 3600), 36.3, -78.3, 153.0, 0.0, 16)]
        self.db = track_db(self.rows)

    def tearDown(self):
        os.unlink(self.db)

    def test_a_window_still_filters(self):
        got = sd.read_track(24, db=self.db)
        self.assertEqual(3, got["count"])
        self.assertNotIn(36.0, got["lat"])

    def test_no_window_returns_the_whole_record(self):
        got = sd.read_track(None, db=self.db)
        self.assertEqual(4, got["count"])
        self.assertEqual(36.0, got["lat"][0])
        self.assertEqual(36.3, got["lat"][-1])

    def test_zero_means_the_whole_record_too(self):
        # the page passes hours=0; 0 must not be read as "nothing" or as an epoch comparison
        self.assertEqual(sd.read_track(None, db=self.db)["count"],
                         sd.read_track(0, db=self.db)["count"])

    def test_oldest_first_and_columnar(self):
        got = sd.read_track(None, db=self.db)
        self.assertEqual(got["lat"], sorted(got["lat"]))
        for k in ("ts", "lat", "lon", "alt", "speed", "sats"):
            self.assertEqual(4, len(got[k]), k)
        # ts must come back as epoch numbers, not the ISO strings the table stores
        self.assertIsInstance(got["ts"][0], float)


class TestTrackThinning(unittest.TestCase):
    def setUp(self):
        # MAX_POINTS + 40 rows, one minute apart, so the cap has to bite
        now = time.time()
        n = sd.MAX_POINTS + 40
        self.rows = [(iso(now - (n - i) * 60), 36.0 + i * 1e-5, -78.0, 150.0, 0.0, 10)
                     for i in range(n)]
        self.db = track_db(self.rows)

    def tearDown(self):
        os.unlink(self.db)

    def test_the_payload_is_bounded(self):
        got = sd.read_track(None, db=self.db)
        self.assertLessEqual(got["count"], sd.MAX_POINTS + 1)
        self.assertGreater(got["stride"], 1)

    def test_the_total_is_reported_honestly(self):
        got = sd.read_track(None, db=self.db)
        self.assertEqual(len(self.rows), got["total"])
        self.assertNotEqual(got["total"], got["count"])   # thinned, and it says so

    def test_the_newest_fix_survives_thinning(self):
        # the panel's amber pin is the last fix: it must be the real one, not `stride` back
        got = sd.read_track(None, db=self.db)
        self.assertAlmostEqual(self.rows[-1][1], got["lat"][-1], places=9)
        self.assertAlmostEqual(36.0 + (len(self.rows) - 1) * 1e-5, got["lat"][-1], places=9)

    def test_an_empty_track_is_not_an_error(self):
        empty = track_db([])
        try:
            got = sd.read_track(None, db=empty)
            self.assertNotIn("error", got)
            self.assertEqual(0, got["count"])
        finally:
            os.unlink(empty)


class TestTrackRoute(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        now = time.time()
        self.rows = [(iso(now - 72 * 3600), 36.0, -78.0, 150.0, 0.0, 10),
                     (iso(now - 1 * 3600), 36.3, -78.3, 153.0, 0.0, 16)]
        self.db = track_db(self.rows)
        # save/restore: unittest discover runs every module in one process
        self._saved = (sd.ROOT, sd.DB)
        sd.ROOT = self.dir
        sd.DB = self.db
        with open(os.path.join(self.dir, "snapshot.json"), "w") as f:
            f.write("{}")
        self.server = sd.Server(("127.0.0.1", 0), sd.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        sd.ROOT, sd.DB = self._saved
        os.unlink(self.db)

    def _get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data)

    def test_hours_zero_serves_the_whole_record(self):
        status, body = self._get("/track?hours=0")
        self.assertEqual(200, status)
        self.assertEqual(2, body["count"])

    def test_the_default_stays_bounded(self):
        # an endpoint that returns everything when asked nothing is a trap on a record
        # that only grows; the page asks for the whole trip explicitly
        status, body = self._get("/track")
        self.assertEqual(200, status)
        self.assertEqual(1, body["count"])

    def test_a_bad_hours_value_falls_back_to_the_default(self):
        status, body = self._get("/track?hours=banana")
        self.assertEqual(200, status)
        self.assertEqual(1, body["count"])

    def test_it_is_no_store(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/track?hours=0")
        r = c.getresponse()
        r.read()
        self.assertEqual("no-store", dict(r.getheaders()).get("Cache-Control"))
        c.close()


if __name__ == "__main__":
    unittest.main()

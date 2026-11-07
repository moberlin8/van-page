"""## Ada 06-Nov-2026 places: a name is an identity, and 3/4 mile is the test (plan §15l).

Matt's rules, 06-Nov-2026:
  * re-using a name means the same place, so it is a new VISIT, not a new place;
  * a name that exists but whose fix is far away is an ERROR, not a new place;
  * no fix, no mark - a null position is worse than a gap;
  * a new name standing on top of an existing place is created, but with a warning.

The threshold is 3/4 mile = 1207 m. These tests pin the boundary, because "how close is close
enough" is the one number in the design that is arbitrary and therefore the one most likely to be
quietly changed.
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

# 36.0570 N, 78.5228 W - the first fix Delilah produced, so the numbers are real.
LAT, LON = 36.057033, -78.522835
# 0.01 deg of latitude is ~1111 m, so 0.0108 is just inside 1207 m and 0.0120 just outside.
NEAR = LAT + 0.0108
FAR = LAT + 0.0120


def make_db(path, rows):
    """Replace the track with `rows` - the tests move the van by rewriting the newest fix."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS gps (ts TEXT PRIMARY KEY, lat REAL, lon REAL, alt REAL,"
                " speed REAL, sats INTEGER, hdop REAL, fix INTEGER)")
    con.execute("DELETE FROM gps")
    for r in rows:
        con.execute("INSERT INTO gps VALUES (?,?,?,?,?,?,?,?)", r)
    con.commit()
    con.close()


class PlacesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.van = os.path.join(self.tmp, "van.db")
        self.places = os.path.join(self.tmp, "places.db")
        make_db(self.van, [("2026-11-07T01:07:44Z", LAT, LON, 154.8, 0.0, 15, 0.8, 3)])

    def mark(self, name, lat=None, lon=None):
        """Mark `name` at the current fix, moving the van first if a latitude is given."""
        if lat is not None:
            make_db(self.van, [("2026-11-07T02:00:00Z", lat, LON if lon is None else lon,
                                150.0, 0.0, 12, 1.0, 3)])
        return sd.place_mark(name, db=self.van, places_db=self.places)


class TestHaversine(unittest.TestCase):
    def test_one_degree_of_latitude_is_about_111_km(self):
        self.assertAlmostEqual(sd.haversine_m(0, 0, 1, 0), 111195, delta=200)

    def test_zero_distance(self):
        self.assertEqual(0.0, sd.haversine_m(LAT, LON, LAT, LON))


class TestNoFix(PlacesBase):
    def test_no_gps_table_is_no_fix_not_an_error(self):
        os.unlink(self.van)
        status, body = sd.place_mark("Campground X", db=self.van, places_db=self.places)
        self.assertEqual(409, status)
        self.assertEqual("no fix", body["error"])

    def test_empty_gps_table_is_no_fix(self):
        make_db(self.van, [])
        status, body = sd.place_mark("Campground X", db=self.van, places_db=self.places)
        self.assertEqual(409, status)
        self.assertEqual("no fix", body["error"])

    def test_no_mark_is_written_when_there_is_no_fix(self):
        make_db(self.van, [])
        sd.place_mark("Campground X", db=self.van, places_db=self.places)
        con = sd.places_con(self.places)
        self.assertEqual(0, con.execute("SELECT COUNT(*) FROM place").fetchone()[0])
        con.close()


class TestNameValidation(PlacesBase):
    def test_empty_name_is_rejected(self):
        self.assertEqual(400, self.mark("")[0])
        self.assertEqual(400, self.mark("   ")[0])
        self.assertEqual(400, self.mark(None)[0])

    def test_long_name_is_rejected(self):
        self.assertEqual(400, self.mark("x" * 65)[0])

    def test_name_is_stripped(self):
        status, body = self.mark("  Campground X  ")
        self.assertEqual(201, status)
        self.assertEqual("Campground X", body["place"]["name"])


class TestNewPlace(PlacesBase):
    def test_first_mark_creates_the_place_and_a_visit(self):
        status, body = self.mark("Campground X")
        self.assertEqual(201, status)
        self.assertTrue(body["new_place"])
        self.assertEqual("2026-11-07T01:07:44Z", body["visit_ts"])
        con = sqlite3.connect(self.places)
        self.assertEqual(1, con.execute("SELECT COUNT(*) FROM place").fetchone()[0])
        self.assertEqual(1, con.execute("SELECT COUNT(*) FROM visit").fetchone()[0])
        self.assertEqual("mark", con.execute("SELECT source FROM visit").fetchone()[0])
        con.close()

    def test_a_new_name_near_an_existing_place_warns(self):
        self.mark("Campground X")
        status, body = self.mark("Campground Y", lat=NEAR)
        self.assertEqual(201, status)
        self.assertIn("warning", body)
        self.assertIn("Campground X", body["warning"])

    def test_a_new_name_far_from_everything_does_not_warn(self):
        self.mark("Campground X")
        status, body = self.mark("Campground Y", lat=FAR)
        self.assertEqual(201, status)
        self.assertNotIn("warning", body)


class TestSamePlace(PlacesBase):
    def test_reusing_a_name_within_the_radius_is_a_visit(self):
        self.mark("Campground X")
        status, body = self.mark("Campground X", lat=NEAR)
        self.assertEqual(200, status)
        self.assertFalse(body["new_place"])
        con = sqlite3.connect(self.places)
        self.assertEqual(1, con.execute("SELECT COUNT(*) FROM place").fetchone()[0])
        self.assertEqual(2, con.execute("SELECT COUNT(*) FROM visit").fetchone()[0])
        con.close()

    def test_reusing_a_name_beyond_the_radius_is_an_error(self):
        self.mark("Campground X")
        status, body = self.mark("Campground X", lat=FAR)
        self.assertEqual(409, status)
        self.assertEqual("too far", body["error"])
        self.assertGreater(body["distance_m"], sd.PLACE_RADIUS_M)
        self.assertEqual("2026-11-07T01:07:44Z", body["nearest_ts"])

    def test_the_error_does_not_create_a_second_place(self):
        self.mark("Campground X")
        self.mark("Campground X", lat=FAR)
        con = sqlite3.connect(self.places)
        self.assertEqual(1, con.execute("SELECT COUNT(*) FROM place").fetchone()[0])
        self.assertEqual(1, con.execute("SELECT COUNT(*) FROM visit").fetchone()[0])
        con.close()

    def test_the_name_match_is_case_insensitive(self):
        self.mark("Campground X")
        status, body = self.mark("campground x", lat=NEAR)
        self.assertEqual(200, status)
        self.assertEqual("Campground X", body["place"]["name"])

    def test_distance_is_measured_to_the_nearest_visit_not_the_first(self):
        # A campground is bigger than a point: mark the gate, then the far end, then come back to
        # the far end. The third mark must be accepted even though it is far from the gate.
        self.mark("Campground X")
        self.mark("Campground X", lat=NEAR)
        status, body = self.mark("Campground X", lat=NEAR)
        self.assertEqual(200, status)


class TestReadPlaces(PlacesBase):
    def test_counts_and_latest_fix(self):
        self.mark("Campground X")
        self.mark("Campground X", lat=NEAR)
        self.mark("Other Spot", lat=FAR)
        out = sd.read_places(places_db=self.places, db=self.van)
        self.assertEqual(2, len(out["places"]))
        by_name = {p["name"]: p for p in out["places"]}
        self.assertEqual(2, by_name["Campground X"]["visits"])
        self.assertEqual(1, by_name["Other Spot"]["visits"])
        self.assertEqual(0, by_name["Campground X"]["surveys"])
        self.assertEqual(sd.PLACE_RADIUS_M, out["radius_m"])
        self.assertIsNotNone(out["latest_fix"])

    def test_empty_registry_is_not_an_error(self):
        out = sd.read_places(places_db=self.places, db=self.van)
        self.assertEqual([], out["places"])


if __name__ == "__main__":
    unittest.main()

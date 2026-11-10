"""## Ada 09-Nov-2026 place-radius tests: the 3/4 mile rule, both directions.

Matt's threshold cuts both ways (his call, 09-Nov-2026). An existing name whose fix is
further than the radius from its own visits is "too far"; a NEW name whose fix lands
within the radius of an existing place is "too close". Both are 409s that write nothing -
a refusal that leaves a place or a visit behind is not a refusal. The near-duplicate case
used to be created with a warning, which is how "Bob" came to exist 2 m from "Ada's Home".
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, "/home/ada/van/bin")
_spec = importlib.util.spec_from_file_location(
    "serve_data", "/home/ada/van/bin/serve-data.py")
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)

BASE_LAT, BASE_LON = 36.057025, -78.522822   # Ada's Home, as filed on 07-Nov-2026
DEG_LAT_M = 111320.0                          # metres per degree of latitude, near enough here


def at(lat_off_m=0.0):
    """A synthetic fix `lat_off_m` metres north of the base position."""
    return {"ts": "2026-11-09T15:00:00Z", "lat": BASE_LAT + lat_off_m / DEG_LAT_M,
            "lon": BASE_LON, "alt": 157.0, "sats": 12, "fix": 3}


class PlaceRadiusCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "places.db")
        sd.places_con(self.db).close()          # the schema, so "nothing was written" is readable
        self._saved_fix = sd.latest_fix
        sd.latest_fix = lambda db=None: self.fix
        self.fix = at()

    def tearDown(self):
        sd.latest_fix = self._saved_fix

    def rows(self, sql):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def places(self):
        return self.rows("SELECT name FROM place ORDER BY id")

    def visits(self):
        return self.rows("SELECT place_id FROM visit ORDER BY id")

    def survey_place(self, run_id):
        return self.rows("SELECT place_id FROM survey WHERE run_id = '%s'" % run_id)


class TestNewName(PlaceRadiusCase):
    def test_new_name_on_top_of_an_existing_place_is_refused(self):
        status, body = sd.place_mark("Ada's Home", places_db=self.db)
        self.assertEqual(status, 201)
        self.assertTrue(body["new_place"])

        status, body = sd.place_mark("Bob", places_db=self.db)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "too close")
        self.assertEqual(body["existing"], "Ada's Home")
        self.assertEqual(body["radius_m"], sd.PLACE_RADIUS_M)
        self.assertIn("already a place", body["detail"])
        # the refusal wrote nothing: one place, one visit, and no "Bob"
        self.assertEqual(self.places(), [("Ada's Home",)])
        self.assertEqual(len(self.visits()), 1)

    def test_just_inside_the_radius_is_refused(self):
        self.assertEqual(sd.place_mark("Ada's Home", places_db=self.db)[0], 201)
        self.fix = at(sd.PLACE_RADIUS_M - 50)      # ~1157 m: inside 3/4 mile
        status, body = sd.place_mark("Bob", places_db=self.db)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "too close")
        self.assertEqual(self.places(), [("Ada's Home",)])

    def test_just_outside_the_radius_is_a_new_place(self):
        self.assertEqual(sd.place_mark("Ada's Home", places_db=self.db)[0], 201)
        self.fix = at(sd.PLACE_RADIUS_M + 50)      # ~1257 m: outside 3/4 mile
        status, body = sd.place_mark("Bob", places_db=self.db)
        self.assertEqual(status, 201)
        self.assertTrue(body["new_place"])
        self.assertEqual(self.places(), [("Ada's Home",), ("Bob",)])

    def test_the_detail_names_the_nearest_place(self):
        self.assertEqual(sd.place_mark("Ada's Home", places_db=self.db)[0], 201)
        self.fix = at(sd.PLACE_RADIUS_M + 100)      # outside the radius: a place of its own
        self.assertEqual(sd.place_mark("Far", places_db=self.db)[0], 201)
        self.fix = at(sd.PLACE_RADIUS_M + 120)      # 20 m from "Far", 1327 m from "Ada's Home"
        status, body = sd.place_mark("Nearly", places_db=self.db)
        self.assertEqual(status, 409)
        self.assertEqual(body["existing"], "Far")
        self.assertLessEqual(body["distance_m"], 25)


class TestExistingName(PlaceRadiusCase):
    def test_re_using_a_name_near_its_visit_is_a_visit(self):
        self.assertEqual(sd.place_mark("Ada's Home", places_db=self.db)[0], 201)
        self.fix = at(700)
        status, body = sd.place_mark("Ada's Home", places_db=self.db)
        self.assertEqual(status, 200)
        self.assertFalse(body["new_place"])
        self.assertAlmostEqual(body["distance_m"], 700, delta=2)
        self.assertEqual(len(self.visits()), 2)

    def test_re_using_a_name_far_away_is_refused(self):
        self.assertEqual(sd.place_mark("Ada's Home", places_db=self.db)[0], 201)
        self.fix = at(3000)
        status, body = sd.place_mark("Ada's Home", places_db=self.db)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "too far")
        self.assertEqual(len(self.visits()), 1)     # the refusal wrote nothing

    def test_a_name_is_matched_case_insensitively(self):
        self.assertEqual(sd.place_mark("Ada's Home", places_db=self.db)[0], 201)
        status, body = sd.place_mark("ada's home", places_db=self.db)
        self.assertEqual(status, 200)
        self.assertEqual(body["place"]["name"], "Ada's Home")


class TestNoFixAndNames(PlaceRadiusCase):
    def test_no_fix_is_refused_without_writing(self):
        self.fix = None
        status, body = sd.place_mark("Anywhere", places_db=self.db)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "no fix")
        self.assertEqual(self.places(), [])

    def test_a_blank_or_overlong_name_is_a_400(self):
        self.assertEqual(sd.place_mark("   ", places_db=self.db)[0], 400)
        self.assertEqual(sd.place_mark("x" * 65, places_db=self.db)[0], 400)
        self.assertEqual(self.places(), [])


class TestSurveyRefusal(PlaceRadiusCase):
    """start_survey files the place first, so it must return the refusal, not paper over it."""

    def _no_launch(self):
        def boom(*a, **k):
            raise AssertionError("the survey engine was launched despite a refusal")
        return boom

    def test_a_near_duplicate_name_starts_nothing(self):
        self.assertEqual(sd.place_mark("Ada's Home", places_db=self.db)[0], 201)
        saved, sd._delilah = sd._delilah, self._no_launch()
        try:
            status, body = sd.start_survey("Bob", places_db=self.db)
        finally:
            sd._delilah = saved
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "too close")
        self.assertEqual(self.places(), [("Ada's Home",)])

    def test_a_far_away_name_starts_nothing(self):
        self.assertEqual(sd.place_mark("Ada's Home", places_db=self.db)[0], 201)
        self.fix = at(5000)
        saved, sd._delilah = sd._delilah, self._no_launch()
        try:
            status, body = sd.start_survey("Ada's Home", places_db=self.db)
        finally:
            sd._delilah = saved
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "too far")

    def test_a_survey_with_no_fix_still_files_the_place(self):
        """A survey does not need a fix: the place exists from the moment it is asked for."""
        self.fix = None
        import types

        def launched(cmd, timeout=20):
            self.assertIn("start-stop-daemon", cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        saved, sd._delilah = sd._delilah, launched
        try:
            status, body = sd.start_survey("Somewhere New", places_db=self.db)
        finally:
            sd._delilah = saved
        self.assertEqual(status, 202)
        self.assertTrue(body["run_id"])
        self.assertEqual(self.places(), [("Somewhere New",)])
        self.assertEqual(self.visits(), [])      # no fix, so no visit - only the place


if __name__ == "__main__":
    unittest.main()

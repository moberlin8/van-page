"""## Ada 09-Nov-2026 tests for the dish-schema cache.

Matt: "catch the tick to stop the flapping". The flap was yagrc's 10 s reflection budget being blown
by a schema download on a WiFi hop that drops 5% of full-MTU packets, re-attempted every 60 s
because poll.py runs in a new process each tick. The cache removes that step; these tests pin the
three things that make it safe to have done so:

  * what is written is read back byte-identically, and a bad or absent cache is a fallback rather
    than an exception;
  * the descriptor files reach the pool in dependency order -- a pool rejects a file whose deps are
    absent, and rejects it permanently, so the order is load-bearing;
  * the version check can never leave a stale schema in place quietly.

The install path needs yagrc (only the poller's venv has it), so those cases skip where it is
absent; everything else runs on the plain suite interpreter.
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import dish_schema  # noqa: E402


def have_yagrc():
    try:
        import yagrc.reflector  # noqa: F401
        return True
    except Exception:
        return False


HAVE_YAGRC = have_yagrc()
NEEDS_YAGRC = unittest.skipUnless(HAVE_YAGRC, "yagrc is in the poller's venv, not this interpreter")


def protoset(files, method_types=("Request", "DishAlerts")):
    """A serialized FileDescriptorSet from (name, package, dependency, message_names, service)."""
    from google.protobuf import descriptor_pb2
    fdset = descriptor_pb2.FileDescriptorSet()
    for name, package, deps, messages, service in files:
        f = fdset.file.add()
        f.name = name
        f.package = package
        f.syntax = "proto3"
        f.dependency.extend(deps)
        for m in messages:
            f.message_type.add().name = m
        if service:
            m = f.service.add()
            m.name = service
            method = m.method.add()
            method.name = "Handle"
            method.input_type = "." + package + "." + method_types[0]
            method.output_type = "." + package + "." + method_types[1]
    return fdset.SerializeToString()


# The real shape, in miniature: a dependency and the service/message mix resolve_imports needs.
DEVICE_FILES = [
    ("deps/base.proto", "SpaceX.API.Base", [], ["Thing"], None),
    ("device.proto", "SpaceX.API.Device", ["deps/base.proto"],
     ["Request", "DishAlerts", "DishOutage"], "Device"),
]


class TestCacheFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = dish_schema.cache_path(self.dir)

    def test_a_missing_cache_is_none_not_an_error(self):
        self.assertIsNone(dish_schema.read_cache(self.path))

    def test_round_trip(self):
        dish_schema.write_cache(self.path, "Zm9v", now=1000.0)
        rec = dish_schema.read_cache(self.path)
        self.assertEqual("Zm9v", rec["protoset"])
        self.assertEqual(1000.0, rec["saved_at"])
        self.assertIsNone(rec["sw_version"])     # no reading has happened yet

    def test_no_temp_file_is_left_behind(self):
        dish_schema.write_cache(self.path, "Zm9v")
        self.assertEqual(["dish-schema.json"], sorted(os.listdir(self.dir)))

    def test_junk_is_refused_rather_than_raised(self):
        for body in ("{not json", "[]", '{"version": 99, "protoset": "x"}', '{"version": 1}'):
            with open(self.path, "w") as f:
                f.write(body)
            self.assertIsNone(dish_schema.read_cache(self.path), body)

    def test_forget_reports_whether_it_had_one(self):
        self.assertFalse(dish_schema.forget(self.path))
        dish_schema.write_cache(self.path, "Zm9v")
        self.assertTrue(dish_schema.forget(self.path))
        self.assertIsNone(dish_schema.read_cache(self.path))

    def test_record_version_stamps_the_cache(self):
        dish_schema.write_cache(self.path, "Zm9v")
        self.assertTrue(dish_schema.record_version(self.path, "2026.10.29.mr90217.1"))
        rec = dish_schema.read_cache(self.path)
        self.assertEqual("2026.10.29.mr90217.1", rec["sw_version"])
        self.assertEqual("Zm9v", rec["protoset"])      # the schema itself is untouched

    def test_record_version_without_a_cache_or_a_version_is_a_no_op(self):
        self.assertFalse(dish_schema.record_version(self.path, "x"))
        dish_schema.write_cache(self.path, "Zm9v")
        self.assertFalse(dish_schema.record_version(self.path, None))


class TestStaleness(unittest.TestCase):
    def test_a_changed_firmware_invalidates(self):
        self.assertTrue(dish_schema.stale_for("old", "new"))

    def test_a_same_firmware_does_not(self):
        self.assertFalse(dish_schema.stale_for("same", "same"))

    def test_unknown_on_either_side_is_not_a_reason_to_throw_it_away(self):
        # before the first reading the cache has no version, and that must not read as "stale"
        self.assertFalse(dish_schema.stale_for(None, "2026.10.29.mr90217.1"))
        self.assertFalse(dish_schema.stale_for("2026.10.29.mr90217.1", None))
        self.assertFalse(dish_schema.stale_for(None, None))


class TestDependencyOrder(unittest.TestCase):
    """The pool refuses a file whose dependencies are absent, so order is not cosmetic."""

    def test_a_dependency_comes_first_even_when_listed_last(self):
        from google.protobuf import descriptor_pb2
        fdset = descriptor_pb2.FileDescriptorSet()
        for name, deps in (("b.proto", ["a.proto"]), ("a.proto", [])):
            f = fdset.file.add()
            f.name, f.syntax = name, "proto3"
            f.dependency.extend(deps)
        protos = dish_schema.descriptors(fdset.SerializeToString())
        self.assertEqual(["a.proto", "b.proto"], dish_schema.dependency_order(protos))

    def test_every_file_appears_exactly_once(self):
        protos = dish_schema.descriptors(protoset(DEVICE_FILES))
        order = dish_schema.dependency_order(protos)
        self.assertEqual(sorted(protos.keys()), sorted(order))
        self.assertEqual(len(protos), len(order))

    def test_an_independent_file_order_is_preserved(self):
        from google.protobuf import descriptor_pb2
        fdset = descriptor_pb2.FileDescriptorSet()
        for name in ("x.proto", "y.proto"):
            f = fdset.file.add()
            f.name, f.syntax = name, "proto3"
        protos = dish_schema.descriptors(fdset.SerializeToString())
        self.assertEqual(["x.proto", "y.proto"], dish_schema.dependency_order(protos))


class TestDescriptors(unittest.TestCase):
    def test_a_protoset_round_trips(self):
        protos = dish_schema.descriptors(protoset(DEVICE_FILES))
        self.assertEqual({"deps/base.proto", "device.proto"}, set(protos))
        self.assertEqual(["deps/base.proto"], list(protos["device.proto"].dependency))

    def test_a_blob_that_is_not_a_protoset_raises(self):
        from google.protobuf.message import DecodeError
        with self.assertRaises(DecodeError):
            dish_schema.descriptors(b"\xff\xff\xff not a protoset")


@NEEDS_YAGRC
class TestInstall(unittest.TestCase):
    def setUp(self):
        self.fake = type("FakeStarlinkGrpc", (), {"imports_pending": True})()

    def test_install_sets_every_name_resolve_imports_sets(self):
        dish_schema.install(self.fake, protoset(DEVICE_FILES))
        self.assertFalse(self.fake.imports_pending)
        for name in ("DeviceStub", "Request", "DishAlerts", "DishOutage"):
            self.assertTrue(hasattr(self.fake, name), name)

    def test_the_classes_are_usable(self):
        dish_schema.install(self.fake, protoset(DEVICE_FILES))
        # a message class can build and serialize the request the poller sends
        self.assertTrue(self.fake.Request().SerializeToString() is not None)
        self.assertTrue(callable(self.fake.DeviceStub))

    def test_a_schema_missing_a_requirement_is_refused(self):
        # self-consistent, so the POOL accepts it and this module's own check is what refuses --
        # a fixture the pool rejects would pass this test without exercising the check at all
        thin = [("device.proto", "SpaceX.API.Device", [], ["Request"], "Device")]
        blob = protoset(thin, method_types=("Request", "Request"))
        with self.assertRaises(ValueError):
            dish_schema.install(self.fake, blob)

    def test_a_schema_with_no_device_service_is_refused(self):
        thin = [("device.proto", "SpaceX.API.Device", [],
                 ["Request", "DishAlerts", "DishOutage"], None)]
        with self.assertRaises(ValueError):
            dish_schema.install(self.fake, protoset(thin))


class TestPrime(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = dish_schema.cache_path(self.dir)
        self.fake = type("FakeStarlinkGrpc", (), {"imports_pending": True})()

    def test_no_cache_reflects_and_writes_one(self):
        if not HAVE_YAGRC:
            self.skipTest("yagrc is in the poller's venv, not this interpreter")
        with mock.patch("yagrc.dump.dump_protocols", return_value=protoset(DEVICE_FILES)) as d:
            mode, sw = dish_schema.prime(self.fake, self.path, None)
        self.assertEqual("fresh", mode)
        self.assertIsNone(sw)
        self.assertEqual(1, d.call_count)
        self.assertIsNotNone(dish_schema.read_cache(self.path))

    def test_a_good_cache_is_used_and_nothing_is_reflected(self):
        if not HAVE_YAGRC:
            self.skipTest("yagrc is in the poller's venv, not this interpreter")
        dish_schema.write_cache(self.path, base64.b64encode(protoset(DEVICE_FILES)).decode())
        with mock.patch("yagrc.dump.dump_protocols",
                        side_effect=AssertionError("must not reflect")) as d:
            mode, _ = dish_schema.prime(self.fake, self.path, None)
        self.assertEqual("cache", mode)
        self.assertEqual(0, d.call_count)

    def test_a_corrupt_cache_is_dropped_and_the_failure_is_not_retried_here(self):
        if not HAVE_YAGRC:
            self.skipTest("yagrc is in the poller's venv, not this interpreter")
        dish_schema.write_cache(self.path, base64.b64encode(b"not a protoset").decode())
        with mock.patch("yagrc.dump.dump_protocols",
                        side_effect=RuntimeError("the link is down")):
            with self.assertRaises(RuntimeError) as cm:
                dish_schema.prime(self.fake, self.path, None)
        # the bad cache is gone, and the tick failed here rather than paying for a shorter retry
        self.assertIsNone(dish_schema.read_cache(self.path))
        self.assertIn("reflection failed", str(cm.exception))

    def test_a_module_fault_still_falls_back(self):
        # the reflection worked, so the path is fine: a schema this module cannot build is OUR bug,
        # and the old behaviour (starlink_grpc reflects for itself) still covers it
        if not HAVE_YAGRC:
            self.skipTest("yagrc is in the poller's venv, not this interpreter")
        thin = protoset([("device.proto", "SpaceX.API.Device", [], ["Request"], "Device")],
                        method_types=("Request", "Request"))
        with mock.patch("yagrc.dump.dump_protocols", return_value=thin):
            mode, sw = dish_schema.prime(self.fake, self.path, None)
        self.assertEqual("reflection", mode)
        self.assertIsNone(sw)
        self.assertTrue(self.fake.imports_pending)       # starlink_grpc will reflect itself
        self.assertIsNone(dish_schema.read_cache(self.path))   # and nothing unusable was cached

    def test_reflection_gets_a_budget_longer_than_the_ten_second_cliff(self):
        if not HAVE_YAGRC:
            self.skipTest("yagrc is in the poller's venv, not this interpreter")
        from yagrc import reflector
        seen = {}

        def capture(channel, filenames=None, symbols=None):
            seen["timeout"] = reflector.QUERY_TIMEOUT
            return protoset(DEVICE_FILES)

        old = reflector.QUERY_TIMEOUT
        try:
            with mock.patch("yagrc.dump.dump_protocols", side_effect=capture):
                dish_schema.prime(self.fake, self.path, None)
            self.assertGreater(seen["timeout"], 10)
            self.assertEqual(old, reflector.QUERY_TIMEOUT)   # and it is put back
        finally:
            reflector.QUERY_TIMEOUT = old


if __name__ == "__main__":
    unittest.main()

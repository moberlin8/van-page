"""## Ada 09-Nov-2026 cache the dish's gRPC schema so the tick stops re-downloading it.

Matt: "catch the tick to stop the flapping".

Why. `starlink_grpc` resolves the whole `SpaceX.API.Device` schema over gRPC *reflection* on the
first call in every process, and `poll.py` runs in a NEW process every 60 s -- so it re-downloaded
that schema every minute, and yagrc caps the reflection at `QUERY_TIMEOUT = 10` s. Measured on
09-Nov-2026: a normal read completes in ~1 s, but five consecutive attempts failed at *exactly*
10.17 s while small-packet pings to the same device showed 0% loss and full-MTU pings were dropping
5% on the WiFi hop. Raising the budget to 60 s made 10/10 succeed. The schema is identical between
ticks, so the heavy multi-packet step was pure waste on a lossy link -- and the only step with a
hard deadline, which is why it was the thing that failed.

So: reflect once, write the schema to disk, and load it from disk afterwards. A tick's only network
step becomes the single small `get_status` call.

Fails soft where it can, and fails fast where it cannot. A failure *inside this module* -- a yagrc
whose internals moved, a schema shape it cannot build -- leaves `starlink_grpc` to reflect for
itself, which is exactly the behaviour before this module existed. A reflection that cannot reach
the device is different: it is re-raised, because `starlink_grpc` would repeat the identical query
at a shorter budget, add its timeout to this one, and fail anyway. (Measured on a dark feed: 45 s
of reflection plus a 10 s retry, 30 s of a 60 s cadence, for a tick that was going to fail.)

The cache is invalidated two ways, so a firmware update cannot leave a stale schema in place
silently: proactively when the reading's `software_version` differs from the one stored beside the
schema, and reactively when a call fails with a protocol error.
"""

import base64
import json
import os
import time

CACHE_VERSION = 1

# The symbols starlink_grpc.resolve_imports() installs, in its order. Keep this list equal to its
# `global` block; a name it expects and this module does not set is a NameError inside a call.
# NOTE Device is a SERVICE and the other three are MESSAGES -- checking them all with
# FindMessageTypeByName is wrong (and was, until the live dish said so).
SYMBOL = "SpaceX.API.Device.Device"
NEEDED_MESSAGES = (
    "SpaceX.API.Device.Request",      # Request
    "SpaceX.API.Device.DishAlerts",   # DishAlerts
    "SpaceX.API.Device.DishOutage",   # DishOutage
)
DEFAULT_REFLECTION_TIMEOUT = 20


def cache_path(var_dir):
    """Where the cached schema lives. One file: the protoset and its metadata together, so the two
    can never be written apart or half-updated by a killed process."""
    return os.path.join(var_dir, "dish-schema.json")


def read_cache(path):
    """The cached record, or None when there is nothing usable there."""
    try:
        with open(path) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("version") != CACHE_VERSION:
        return None
    if not rec.get("protoset"):
        return None
    return rec


def write_cache(path, protoset_b64, now=None):
    """Write the record atomically. A torn cache is worse than none, so write then replace."""
    rec = {"version": CACHE_VERSION,
           "saved_at": now if now is not None else time.time(),
           "sw_version": None,
           "protoset": protoset_b64}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, path)


def record_version(path, sw_version):
    """Store the firmware the schema is good for, once a reading has told us what that is.

    The schema is cached before any reading exists -- making the read cheap is the whole point --
    so the version arrives one step later, on the first successful call. Without it a firmware
    update that moved the protocol would leave a stale schema in place with nothing to compare it
    against, and the symptom would be a protocol error rather than a refresh.
    """
    if not sw_version:
        return False
    rec = read_cache(path)
    if rec is None:
        return False
    rec["sw_version"] = str(sw_version)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, path)
    return True


def forget(path):
    """Drop the cache so the next tick reflects again. Returns True if there was one."""
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def descriptors(protoset_blob):
    """The FileDescriptorProtos in a serialized FileDescriptorSet, keyed by file name.

    Needs only protobuf, not yagrc, so the tests for the ordering below run anywhere.
    """
    from google.protobuf import descriptor_pb2
    fdset = descriptor_pb2.FileDescriptorSet.FromString(protoset_blob)
    return {p.name: p for p in fdset.file}


def dependency_order(protos):
    """File names, each after everything it depends on.

    This is yagrc's own walk (`GrpcReflectionEngine.load_protocols`, the loop after the reflection
    exchange), lifted here because a descriptor pool rejects a file whose dependencies are absent
    and refuses it permanently -- so the order is load-bearing, and it is the one part of the
    install that can be tested without a gRPC stack.
    """
    from collections import deque
    names = deque(protos.keys())
    traversed = set()
    order = []
    while names:
        name = names[0]
        traversed.add(name)
        deps = set(protos[name].dependency) - traversed
        if deps:
            names = deque(x for x in names if x not in deps)
            names.extendleft(deps)
        else:
            del names[0]
            order.append(name)
    return order


def install(starlink_grpc, protoset_blob):
    """Install the schema into `starlink_grpc`'s module globals from a serialized FileDescriptorSet.

    Replicates the half of `yagrc.reflector.GrpcReflectionEngine.load_protocols` that runs *after*
    the reflection exchange: fill the method map, add each file to the descriptor pool in dependency
    order, then build the classes. yagrc has no loader for a protoset it wrote (`yagrc.dump` writes
    one; nothing reads it back), which is why this exists here rather than being a call.

    Takes the module rather than importing it, so a test can pass a stand-in and never needs the
    gRPC stack. Raises on anything unexpected; the caller treats that as "use the cache I do not
    have".
    """
    from yagrc import reflector

    protos = descriptors(protoset_blob)
    engine = reflector.GrpcReflectionEngine()
    for proto in protos.values():
        engine.methods_by_file[proto.name] = {s.name: s.method for s in proto.service}
    for name in dependency_order(protos):
        engine.pool.Add(protos[name])

    for symbol in NEEDED_MESSAGES:
        try:
            engine.pool.FindMessageTypeByName(symbol)
        except KeyError:
            raise ValueError("cached schema has no message %s" % symbol)
    try:
        service = engine.pool.FindServiceByName(SYMBOL)
    except KeyError:
        raise ValueError("cached schema has no service %s" % SYMBOL)
    methods = engine.methods_by_file.get(service.file.name, {}).get(service.name)
    if methods is None:
        raise ValueError("cached schema has no methods for %s" % SYMBOL)
    stub_class = engine.gen_stub_class(service, methods)[0]

    def msg(name):
        return engine.message_from_descr(engine.pool.FindMessageTypeByName(name))

    starlink_grpc.DeviceStub = stub_class
    starlink_grpc.Request = msg("SpaceX.API.Device.Request")
    starlink_grpc.DishAlerts = msg("SpaceX.API.Device.DishAlerts")
    starlink_grpc.DishOutage = msg("SpaceX.API.Device.DishOutage")
    starlink_grpc.imports_pending = False
    return True


def prime(starlink_grpc, path, channel, now=None, reflection_timeout=DEFAULT_REFLECTION_TIMEOUT):
    """Make the schema ready for this process. Returns (mode, cached_sw_version).

    mode is:
      "cache"       -- loaded from disk; the tick's next step is the get_status call alone
      "fresh"       -- reflected now (because there was no usable cache) and written for next time
      "reflection"  -- this module could not help; starlink_grpc will reflect for itself, i.e. the
                       behaviour that existed before it
    """
    from yagrc import dump
    from yagrc import reflector

    rec = read_cache(path)
    if rec:
        try:
            install(starlink_grpc, base64.b64decode(rec["protoset"]))
            return "cache", rec.get("sw_version")
        except Exception:
            forget(path)          # corrupt or incompatible: reflect, and do not keep it

    try:
        # The cold path only. It is no longer the 60-second hot path, so it can afford a budget that
        # survives a bad window on this link instead of dying at 10 s.
        old = reflector.QUERY_TIMEOUT
        reflector.QUERY_TIMEOUT = reflection_timeout
        try:
            blob = dump.dump_protocols(channel, symbols=[SYMBOL])
        finally:
            reflector.QUERY_TIMEOUT = old
    except Exception as e:
        # The path or the device is the problem. Do NOT fall back to starlink_grpc's own reflection:
        # it would repeat the identical query at a SHORTER budget, so it can only fail too, and it
        # would add its timeout to this one -- measured at 45 + 10 = 30 s of a 60 s cadence, on a
        # feed that is already dark. Fail the tick here instead.
        raise RuntimeError("dish schema reflection failed after %gs: %s"
                           % (reflection_timeout, e)) from e

    try:
        install(starlink_grpc, blob)
    except Exception as e:
        # THIS module's bug (a yagrc whose internals moved, a schema shape it cannot build), which
        # is the case the old behaviour still covers. Leave imports_pending alone and say so on
        # stderr: poll.py logs its stderr on a failed tick, and a silent fallback would hide a bug
        # in this module behind "the dish flapped again".
        import sys
        sys.stderr.write("[dish_schema] %s: %s -- falling back to reflection\n"
                         % (type(e).__name__, e))
        return "reflection", None

    try:
        write_cache(path, base64.b64encode(blob).decode("ascii"), now=now)
    except OSError:
        pass                      # a cache we could not write is a slow tick, not a failed one
    return "fresh", None


def stale_for(cached_sw, seen_sw):
    """Has the dish's firmware moved since the schema was cached?

    A protocol change under a stale schema shows up as a parse or attribute error deep in a call, so
    the version it was cached against is compared on every successful read; a mismatch drops the
    cache and the next tick reflects. Unknown on either side is treated as "cannot tell", not as a
    reason to throw the cache away.
    """
    if not cached_sw or not seen_sw:
        return False
    return str(cached_sw) != str(seen_sw)

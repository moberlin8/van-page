#!/usr/bin/env python3
"""## Ada 22-Oct-2026 serve /srv/van/data as plain static JSON.

Next.js (the homepage container) will not serve arbitrary files from
/app/public/van/ -- it 404s /van/data/snapshot.json, /van/status.json and any
other .json it does not know about, while happily serving .ttf and .html. The
page therefore reads its data from a separate origin instead of fighting the
framework. This is a small static server on 127.0.0.1:8891; tailscaled
terminates TLS for it on the tailnet (port 8891 -> 127.0.0.1:8891).

Ada 23-Oct-2026 added /history: the poller writes one row per tick into
/srv/van/data/van.db, so the page draws its scrolling charts from the same file
the snapshot comes from. Read-only, and the column list is a whitelist -- no
user input ever reaches the query string.

Only ever binds loopback: the data was read-only until Ada 02-Nov-2026, which
added the shared alert-dismissal store (dismissed.json) as the one writable
file; the tailnet is the only door in.

Ada 02-Nov-2026 the alerts now live 24h and are dismissible (Matt), so this
server owns dismissed.json: the map of closed alert ids, shared by every
device. It is a separate file from snapshot.json on purpose -- the poller
rewrites snapshot.json every tick, so a dismissal kept there would be
overwritten and the closed alert would come back. Routes: GET /dismissed
(no-store), POST /dismiss {id}, POST /dismiss_all {ids:[...]}.
"""
import http.server
import json
import os
import posixpath
import socketserver
import sqlite3
import tempfile
import threading
import time
import urllib.parse

ROOT = "/srv/van/data"
DB = os.path.join(ROOT, "van.db")
HOST = "127.0.0.1"
PORT = 8891

# Ada 02-Nov-2026 the shared alert-dismissal store (Matt). Deliberately a
# SEPARATE file from snapshot.json: the poller rewrites snapshot.json every tick
# from a copy it read at the start of that tick, so a dismissal landing mid-tick
# would be overwritten and the closed alert would come back. The poller never
# opens this file.
#
# It is NOT under /srv/van/data, where the snapshot lives, because
# van-data.service runs with ReadOnlyPaths=/srv/van/data on purpose ("this only
# reads it" -- the poller is the writer there). Granting this service write
# access to that directory would let a bug here rewrite van.db. This file is a
# writer's file, so it lives with the runtime state the server owns; the sandbox
# on the poller's data stays exactly as it was.
DISMISSED = "/home/ada/van/var/dismissed.json"
DISMISS_WINDOW_S = 26 * 3600


def read_dismissed(path=None):
    """The shared dismissal map {id: dismissed_ts}.

    A missing, unreadable or malformed file is an empty map, never an error:
    the page has to render whether or not anyone has dismissed anything.
    """
    path = DISMISSED if path is None else path
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return {}
    ids = data.get("ids") if isinstance(data, dict) else None
    if not isinstance(ids, dict):
        return {}
    out = {}
    for k, v in ids.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def prune_dismissed(ids, now=None):
    """Drop ids dismissed longer ago than the retention window.

    A timestamp in the future (browser/poller clock skew) is kept, not dropped:
    a dismissal that appears to be from tomorrow is still a dismissal.
    """
    now = time.time() if now is None else now
    return {k: v for k, v in ids.items() if now - v <= DISMISS_WINDOW_S}


def write_dismissed(ids, path=None):
    """Atomic write: a UNIQUE temp file in the same dir, then os.replace.

    The temp name must be unique per writer. A fixed `path + ".tmp"` lets two
    overlapping writers share one temp path, so one thread's os.replace finds the
    file already moved and raises FileNotFoundError (observed under concurrency).
    """
    path = DISMISSED if path is None else path
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".dismissed-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"ids": ids}, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Ada 02-Nov-2026 the server is a ThreadingTCPServer and the page does NOT await
# its POSTs, so dismissing one row and then hitting "close all" fires two
# overlapping writes. Without this lock the read-modify-write loses updates --
# a probe measured 277 of 320 dismissals lost, and a lost id comes back on the
# next /dismissed fetch (a written anti-goal of the design).
_dismiss_lock = threading.Lock()


def dismiss_ids(id_list, path=None, now=None):
    """Add ids to the map, prune, write, and return the new map.

    The whole read-modify-write runs under one lock; the unique temp name in
    write_dismissed covers the case where two writers still meet.
    """
    now = time.time() if now is None else now
    with _dismiss_lock:
        ids = read_dismissed(path)
        for i in id_list:
            if isinstance(i, str) and i:
                ids[i] = now
        ids = prune_dismissed(ids, now=now)
        write_dismissed(ids, path)
    return ids

# Whitelist of van.db columns the page may ask for. Keys are what the request
# uses; values are the real column names. A requested field that is not in here
# is dropped rather than echoed into SQL.
FIELDS = {
    "ping_drop": "ping_drop",
    "ping_drop_15m": "ping_drop_15m",
    "ping_latency": "ping_latency",
    "downlink_bps": "downlink_bps",
    "uplink_bps": "uplink_bps",
    "snr": "snr",
    "frac_obstruct": "frac_obstruct",
    "obstructed": "obstructed",
    "latency_mean": "latency_mean",
    "latency_p90": "latency_p90",
    "power_w": "power_w",
    "elevation": "elevation",
    "azimuth": "azimuth",
    "gps_sats": "gps_sats",
    "samples_15m": "samples_15m",
    "usage_down": "usage_down",
    "usage_up": "usage_up",
    "alerts": "alerts",
    # Ada 23-Oct-2026 rolling averages of "prolonged" obstruction events
    "obstruction_duration": "obstruction_duration",
    "obstruction_interval": "obstruction_interval",
}

# The page draws ~16h at a 60s tick, so ~1000 points. Above this the answer is
# thinned in Python rather than shipped whole, which keeps the payload (and the
# browser's render) bounded however long the poller has been running.
MAX_POINTS = 1500

# Ada 23-Oct-2026 the obstruction map. The dish exposes a 123x123 grid of
# signal quality per direction (0.0 = obstructed, 1.0 = clear, -1.0 = no data),
# and starlink-grpc-tools already knows how to pull it. It is rendered here as
# a PNG so the page needs no canvas code for it. Colours are the page's own
# zone colours, and "no data" stays genuinely transparent -- filling it with a
# solid colour would make it read as obstruction and the map would lie.
GRPC_TOOLS = "/home/ada/starlink/starlink-grpc-tools"
DISH_TARGET = "127.0.0.1:19200"
OBSTRUCTED_RGB = (0xFB, 0x71, 0x85)   # rose-400    = the page's --bad
CLEAR_RGB = (0x34, 0xD3, 0x99)        # emerald-400 = the page's --ok
OBSTRUCTION_TTL = 60
_obstruction_cache = {"ts": 0.0, "png": None}


def render_obstruction_map():
    """Return (png_bytes, error_str). Cached for OBSTRUCTION_TTL seconds."""
    now = time.time()
    if _obstruction_cache["png"] and now - _obstruction_cache["ts"] < OBSTRUCTION_TTL:
        return _obstruction_cache["png"], None

    try:
        import io
        import sys
        sys.path.insert(0, GRPC_TOOLS)
        import png          # pypng, present in the starlink venv
        import starlink_grpc
    except Exception as e:  # missing dep or tools dir
        return None, "cannot load map renderer: %s" % e

    try:
        grid = starlink_grpc.obstruction_map(
            context=starlink_grpc.ChannelContext(DISH_TARGET))
    except Exception as e:
        return None, "dish map failed: %s" % e

    if not grid or not grid[0]:
        return None, "dish returned an empty map"

    ob_r, ob_g, ob_b = OBSTRUCTED_RGB
    cl_r, cl_g, cl_b = CLEAR_RGB
    rows = []
    for row in grid:
        px = bytearray()
        for v in row:
            if v < 0:
                px += b"\x00\x00\x00\x00"          # no data: fully transparent
                continue
            t = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
            px += bytes((
                round(ob_r + (cl_r - ob_r) * t),
                round(ob_g + (cl_g - ob_g) * t),
                round(ob_b + (cl_b - ob_b) * t),
                255,
            ))
        rows.append(bytes(px))

    buf = io.BytesIO()
    try:
        # greyscale must be passed explicitly: pypng's Default sentinel resolves
        # alpha=True on its own to greyscale+alpha (2 channels), which rejects a
        # 4-byte-per-pixel row.
        png.Writer(len(grid[0]), len(grid), greyscale=False, alpha=True).write(buf, rows)
    except Exception as e:
        return None, "cannot encode map: %s" % e
    data = buf.getvalue()
    _obstruction_cache["ts"] = now
    _obstruction_cache["png"] = data
    return data, None


def read_history(hours, fields):
    """Columnar history from van.db. Returns (payload_dict, error_str)."""
    if not os.path.exists(DB):
        return None, "no history database yet"
    want = [f for f in fields if f in FIELDS]
    if not want:
        return None, "no usable fields requested"

    cols = ", ".join(["ts"] + [FIELDS[f] for f in want])
    since = time.time() - hours * 3600

    # mode=ro: never create or write the poller's file. uri=True is required for
    # the query-string form; timeout lets a poll in flight finish first.
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    except sqlite3.Error as e:
        return None, "cannot open history: %s" % e
    try:
        rows = con.execute(
            "SELECT %s FROM dish WHERE ts >= ? ORDER BY ts ASC" % cols, (since,)
        ).fetchall()
    except sqlite3.Error as e:
        return None, "cannot read history: %s" % e
    finally:
        con.close()

    if not rows:
        return None, "no samples in the last %g h" % hours

    # Thin only when there is genuinely too much to draw.
    stride = max(1, int(len(rows) / MAX_POINTS) + 1)
    if stride > 1:
        rows = rows[::stride]

    out = {"hours": hours, "count": len(rows),
           "from": rows[0][0], "to": rows[-1][0], "ts": [r[0] for r in rows]}
    for i, f in enumerate(want):
        out[f] = [None if r[i + 1] is None else float(r[i + 1]) for r in rows]
    return out, None


# Ada 24-Oct-2026 the billing period. Starlink bills on a cycle anchored to the
# day the kit was first activated, NOT the calendar month: Matt's account runs
# the 25th of one month to the 24th of the next (the Starlink help page shows
# "September 25 - October 24" as the example, and his account shows the same).
# The dish does not report this -- it is not in the gRPC status fields -- so it
# is a constant here. Change it if the account's billing day changes.
BILLING_DAY = 25

# Ada 06-Nov-2026 the one manual figure in this file, scoped to the period it belongs to.
# The database's first dish sample is 20-Oct-2026, so the Oct 21-24 roaming traffic has no
# rows and was added as a flat 15.8 GB (split by the down/up ratio of the real Oct 22-24
# data). It was added to EVERY period, which from 25-Oct meant counting the previous
# cycle's usage into this one: the page read 68.5 GB where the account read 51 GB, and the
# lump is the whole gap. It is now applied only when the period actually contains those days.
MANUAL_WINDOW = ("2026-10-21", "2026-10-25")   # UTC, [start, end)
MANUAL_TOTAL = 15.8e9
MANUAL_SPLIT = (3.541 / 5.494, 1.953 / 5.494)   # down, up


# Ada 06-Nov-2026 which terminal a dish row came from. Two Starlink terminals answer the same
# tcp address, so the tile follows whichever wins the route and their counters have unrelated
# baselines; summing across a switch counts the difference between two kits as traffic.
# `dish_id` is the exact answer once the poller stores it (06-Nov onward). Before that the
# firmware string is the only marker that exists, so it is the fallback - coarse, but it is the
# difference that produced the 0.78 GB of phantom in the 06-Nov reconciliation.
def terminal_group(sw_version, dish_id):
    """Identity to group usage by: the stored device id, else the firmware string, else None."""
    if dish_id:
        return dish_id
    if sw_version:
        return sw_version
    return None


def manual_lump_for(start, end):
    """(down, up) of the un-covered Oct 21-24 roaming, or (0.0, 0.0) if absent.

    `start`/`end` are period bounds as epoch seconds (UTC), inclusive end.
    """
    import datetime as _dt
    w0 = _dt.datetime.strptime(MANUAL_WINDOW[0], "%Y-%m-%d").replace(
        tzinfo=_dt.timezone.utc).timestamp()
    w1 = _dt.datetime.strptime(MANUAL_WINDOW[1], "%Y-%m-%d").replace(
        tzinfo=_dt.timezone.utc).timestamp()
    if end < w0 or start >= w1:
        return (0.0, 0.0)
    return (MANUAL_TOTAL * MANUAL_SPLIT[0], MANUAL_TOTAL * MANUAL_SPLIT[1])


def billing_period(now=None):
    """Return (start_ts, end_ts) of the current Starlink billing period, UTC."""
    import calendar
    import datetime as _dt
    now = _dt.datetime.fromtimestamp(now if now is not None else time.time(),
                                     tz=_dt.timezone.utc)
    start = now.replace(day=BILLING_DAY, hour=0, minute=0, second=0, microsecond=0)
    if start > now:
        # we are before this month's billing day, so the period began last month
        first = (start.replace(day=1) - _dt.timedelta(days=1)).replace(
            day=BILLING_DAY, hour=0, minute=0, second=0, microsecond=0)
        start = first
    nxt = start + _dt.timedelta(days=32)
    nxt = nxt.replace(day=BILLING_DAY, hour=0, minute=0, second=0, microsecond=0)
    end = nxt - _dt.timedelta(seconds=1)
    return start.timestamp(), end.timestamp()


def read_usage(db=None):
    """Total bytes moved in the current billing period.

    `db` defaults to None and resolves the module global in the body, so a test can point
    it at a temporary store without patching this module's state (see tests/).

    Ada 24-Oct-2026 the dish's usage counters are ROLLING, not cumulative: over
    the 2 days in the database usage_down gained 3301 MB and lost 3297 MB, so the
    counter's current value is not a period total. The trustworthy measure is the
    sum of the increases, which is real bytes moved. Small drops lose a little,
    so the result is a slight over-count.
    """
    if db is None:
        db = DB
    if not os.path.exists(db):
        return None, "no database yet"
    start, end = billing_period()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    except sqlite3.Error as e:
        return None, "cannot open usage: %s" % e
    try:
        # Ada 06-Nov-2026 select the identity columns only if they exist: the poller migrates
        # the table on its next tick, and /usage must keep answering in the window before that
        # (a legacy store groups by firmware string, which is all it has).
        have = {r[1] for r in con.execute("PRAGMA table_info(dish)")}
        cols = ["ts", "usage_down", "usage_up"]
        for name in ("sw_version", "dish_id", "dish_hw"):
            cols.append(name if name in have else "NULL AS " + name)
        rows = con.execute(
            "SELECT %s FROM dish WHERE ts >= ? ORDER BY ts ASC" % ", ".join(cols),
            (start,)).fetchall()
    except sqlite3.Error as e:
        return None, "cannot read usage: %s" % e
    finally:
        con.close()
    if not rows:
        return None, "no samples in this billing period yet"

    # Ada 06-Nov-2026 accumulate INSIDE each terminal. A switch starts that terminal's own
    # prev trackers, so the step between two kits' unrelated baselines is never counted.
    groups = {}
    for ts, d, u, sw, did, dhw in rows:
        key = terminal_group(sw, did)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"id": did, "hw": dhw, "label": key or "unknown",
                               "down": 0.0, "up": 0.0, "samples": 0,
                               "first": ts, "last": ts, "prev_down": None, "prev_up": None}
        if g["prev_down"] is not None and d is not None and d > g["prev_down"]:
            g["down"] += d - g["prev_down"]
        if g["prev_up"] is not None and u is not None and u > g["prev_up"]:
            g["up"] += u - g["prev_up"]
        if d is not None:
            g["prev_down"] = d
        if u is not None:
            g["prev_up"] = u
        if did and not g["id"]:
            g["id"], g["hw"] = did, dhw
        g["samples"] += 1
        g["last"] = ts

    down = sum(g["down"] for g in groups.values())
    up = sum(g["up"] for g in groups.values())
    terminals = [{k: g[k] for k in ("id", "hw", "label", "down", "up",
                                    "samples", "first", "last")}
                 for g in sorted(groups.values(), key=lambda x: x["down"], reverse=True)]

    now = time.time()
    # Ada 06-Nov-2026 the lump, only for a period that contains Oct 21-24 (see MANUAL_WINDOW).
    manual_down, manual_up = manual_lump_for(start, end)
    down += manual_down
    up += manual_up
    return {
        "billing_day": BILLING_DAY,
        "period_start": start,
        "period_end": end,
        "days_total": int(round((end - start) / 86400.0)),
        "days_used": max(0, int(round((now - start) / 86400.0))),
        "days_left": max(0, int(round((end - now) / 86400.0))),
        "down": down,
        "up": up,
        "total": down + up,
        # The database only started collecting on 22-Oct-2026, so the period is
        # covered from then -- the page says so rather than claiming a full month.
        "covered_from": rows[0][0],
        "covered_to": rows[-1][0],
        "samples": len(rows),
        # Ada 06-Nov-2026 per-terminal detail: the total is the sum of these, so the page can
        # name which terminal it is reading instead of silently using both.
        "terminals": terminals,
        # Ada 06-Nov-2026 say whether the manual figure was counted, and how much of it,
        # so nobody has to read this file to know what the total is made of.
        "manual_lump": {
            "applied": bool(manual_down or manual_up),
            "down": manual_down,
            "up": manual_up,
            "window": "%s..%s" % MANUAL_WINDOW,
        },
    }, None


class Handler(http.server.SimpleHTTPRequestHandler):
    # Ada 02-Nov-2026 per-request opt-out of the 60s cache. snapshot.json wants
    # the cache; the dismissal state must never be cached or the page hands back
    # pre-dismissal state and a closed alert flashes back.
    # This is a CLASS attribute, not set in __init__: BaseRequestHandler.__init__
    # calls handle() itself, so the request has already been served by the time
    # __init__ gets a line after super().__init__() -- an instance attribute set
    # there is too late and end_headers() raises AttributeError.
    no_store = False

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def translate_path(self, path):
        # no traversal: strip query, normalise, and keep it inside ROOT
        path = urllib.parse.urlparse(path).path
        path = posixpath.normpath(path)
        words = [w for w in path.split("/") if w and w not in (".", "..")]
        full = os.path.join(ROOT, *words)
        return full

    def end_headers(self):
        # the page lives on :8888 and fetches here on :8891 -- different ports
        # are different origins, so the browser blocks the response without an
        # explicit CORS header. The tailnet is the only door in, so a permissive
        # allow-origin is correct here. Ada 02-Nov-2026 POST is allowed now that
        # the dismissal routes write; Content-Type was already permitted, which
        # is what a JSON POST preflight needs.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # the page polls every 15s; never let a cache answer that
        # Ada 23-Oct-2026 the map is a slow-changing snapshot, so let the
        # browser cache it: without this a 15s rebuild would refetch it and
        # the disc would visibly flicker. 60s matches the server-side TTL.
        self.send_header("Cache-Control",
                         "no-store" if self.no_store else "max-age=60")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    # Ada 23-Oct-2026 a HEAD probe used to get a 404 here: SimpleHTTPRequestHandler
    # has its own do_HEAD that never routes through do_GET, so /obstruction.png
    # answered "not found" to HEAD while GET returned 200 -- a health check would
    # have reported the endpoint dead. Route HEAD the same way, headers only.
    def _respond(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/")

        # Ada 02-Nov-2026 the dismissal writes. /dismiss_all takes the ids the
        # page had on screen, NOT "everything in the window": an alert that
        # fired between render and click must not be swallowed unseen.
        if path in ("/dismiss", "/dismiss_all"):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                self._respond(400, "application/json; charset=utf-8",
                              json.dumps({"error": "bad json"}).encode("utf-8"))
                return
            try:
                if path == "/dismiss":
                    if not isinstance(payload.get("id"), str) or not payload["id"]:
                        self._respond(400, "application/json; charset=utf-8",
                                      json.dumps({"error": "id required"}).encode("utf-8"))
                        return
                    ids = dismiss_ids([payload["id"]])
                    body = {"ok": True, "ids": ids}
                else:
                    wanted = [i for i in (payload.get("ids") or [])
                              if isinstance(i, str) and i]
                    ids = dismiss_ids(wanted)
                    body = {"ok": True, "count": len(wanted), "ids": ids}
            except OSError as e:
                # A store that cannot be written has to be LOUD. An empty reply,
                # or a silent success, leaves the page looking like the alert was
                # dismissed when it was not -- reported as "dismiss does nothing"
                # with no way to tell why from the browser.
                self._respond(500, "application/json; charset=utf-8",
                              json.dumps({"error": "cannot write dismissal store: %s" % e}).encode("utf-8"))
                return
            self.no_store = True
            self._respond(200, "application/json; charset=utf-8",
                          json.dumps(body).encode("utf-8"))
            return

        if path == "/history":
            self._respond(405, "application/json; charset=utf-8",
                          json.dumps({"error": "method not allowed"}).encode("utf-8"))
            return
        if path == "/usage":
            self._respond(405, "application/json; charset=utf-8",
                          json.dumps({"error": "method not allowed"}).encode("utf-8"))
            return
        return super().do_GET()


    def do_GET(self):
        u = urllib.parse.urlparse(self.path)

        if u.path.rstrip("/") in ("/dismissed", "/dismissed.json"):
            self.no_store = True
            body = json.dumps({"ids": read_dismissed()}).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body)
            return

        if u.path.rstrip("/") in ("/obstruction.png", "/obstruction"):
            data, err = render_obstruction_map()
            if err or not data:
                raw = json.dumps({"error": err or "no map"}).encode("utf-8")
                self._respond(503, "application/json; charset=utf-8", raw)
                return
            self._respond(200, "image/png", data)
            return

        if u.path.rstrip("/") in ("/history", "/history.json"):
            q = urllib.parse.parse_qs(u.query)
            try:
                hours = float(q.get("hours", ["16"])[0])
            except ValueError:
                hours = 16.0
            hours = max(0.25, min(24 * 30, hours))
            fields = [f for f in q.get("fields", [""])[0].split(",") if f]

            payload, err = read_history(hours, fields)
            if err:
                self._respond(404, "application/json; charset=utf-8",
                              json.dumps({"error": err, "hours": hours}).encode("utf-8"))
            else:
                self._respond(200, "application/json; charset=utf-8",
                              json.dumps(payload).encode("utf-8"))
            return

        if u.path.rstrip("/") in ("/usage", "/usage.json"):
            payload, err = read_usage()
            if err:
                self._respond(404, "application/json; charset=utf-8",
                              json.dumps({"error": err}).encode("utf-8"))
            else:
                self._respond(200, "application/json; charset=utf-8",
                              json.dumps(payload).encode("utf-8"))
            return

        # anything else is a plain static file from ROOT (snapshot.json)
        return super().do_GET()

    def log_message(self, fmt, *args):
        pass  # keep the journal quiet; the poller logs are the signal


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server((HOST, PORT), Handler) as httpd:
        print("serving %s on http://%s:%d" % (ROOT, HOST, PORT), flush=True)
        httpd.serve_forever()

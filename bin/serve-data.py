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

Only ever binds loopback: the data is read-only and the tailnet is the only
door in.
"""
import http.server
import json
import os
import posixpath
import socketserver
import sqlite3
import time
import urllib.parse

ROOT = "/srv/van/data"
DB = os.path.join(ROOT, "van.db")
HOST = "127.0.0.1"
PORT = 8891

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


def read_usage():
    """Total bytes moved in the current billing period.

    Ada 24-Oct-2026 the dish's usage counters are ROLLING, not cumulative: over
    the 2 days in the database usage_down gained 3301 MB and lost 3297 MB, so the
    counter's current value is not a period total. The trustworthy measure is the
    sum of the increases, which is real bytes moved. Small drops lose a little,
    so the result is a slight over-count.
    """
    if not os.path.exists(DB):
        return None, "no database yet"
    start, end = billing_period()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    except sqlite3.Error as e:
        return None, "cannot open usage: %s" % e
    try:
        rows = con.execute(
            "SELECT ts, usage_down, usage_up FROM dish WHERE ts >= ?"
            " ORDER BY ts ASC", (start,)).fetchall()
    except sqlite3.Error as e:
        return None, "cannot read usage: %s" % e
    finally:
        con.close()
    if not rows:
        return None, "no samples in this billing period yet"

    down = up = 0.0
    prev_d = prev_u = None
    for ts, d, u in rows:
        if prev_d is not None and d is not None and d > prev_d:
            down += d - prev_d
        if prev_u is not None and u is not None and u > prev_u:
            up += u - prev_u
        if d is not None:
            prev_d = d
        if u is not None:
            prev_u = u

    now = time.time()
    # Ada 01-Nov-2026 add manual Oct 21-24 roaming totals (split by the same
    # down/up ratio as the real Oct 22-24 data). The dish had no samples before
    # Oct 22, so those four days are not represented in the DB.
    manual_down = 15.8e9 * (3.541 / 5.494)
    manual_up = 15.8e9 * (1.953 / 5.494)
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
    }, None


class Handler(http.server.SimpleHTTPRequestHandler):
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
        # explicit CORS header. The tailnet is the only door in, and the data
        # is read-only, so a permissive allow-origin is correct here.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # the page polls every 15s; never let a cache answer that
        # Ada 23-Oct-2026 the map is a slow-changing snapshot, so let the
        # browser cache it: without this a 15s rebuild would refetch it and
        # the disc would visibly flicker. 60s matches the server-side TTL.
        self.send_header("Cache-Control", "max-age=60")
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

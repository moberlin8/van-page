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
import datetime
import http.server
import json
import math
import os
import posixpath
import shlex
import subprocess
import socketserver
import sqlite3
import tempfile
import threading
import time
import urllib.parse

ROOT = "/srv/van/data"

# Ada 06-Nov-2026 the page's own commit, so the bottom of the page can say which build is being
# looked at (Matt). The page is served from a git working tree that is bind-mounted into the
# homepage container, so the answer is read from that tree at request time rather than baked into
# the HTML: a hard-coded SHA is stale the moment the commit that writes it lands - which is
# exactly the commit you would want to see.
PAGE_REPO = "/srv/van"
_VERSION_CACHE = {"at": 0.0, "value": None}


def page_version(repo=None, max_age=10.0):
    """{'commit': short sha, 'full': sha, 'branch': name} for the page's working tree.

    Cached for `max_age` seconds so a page load does not shell out to git on every request, and
    `max_age=0` forces a fresh read (the tests use that). A directory that is not a repo, or a
    missing git, returns {'error': ...} rather than raising: the footer is decoration, and it must
    never be able to break the page.
    """
    repo = PAGE_REPO if repo is None else repo
    now = time.time()
    if max_age and _VERSION_CACHE["value"] is not None and now - _VERSION_CACHE["at"] < max_age:
        return _VERSION_CACHE["value"]

    def git(*args):
        return subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                              text=True, timeout=5).stdout.strip()

    try:
        full = git("rev-parse", "HEAD")
        if not full:
            return {"error": "not a git working tree: %s" % repo}
        value = {"commit": full[:7], "full": full,
                 "branch": git("rev-parse", "--abbrev-ref", "HEAD")}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": "cannot read the page's commit: %s" % e}
    _VERSION_CACHE.update({"at": now, "value": value})
    return value
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

# Ada 06-Nov-2026 places: a name, the visits to it, and the surveys run there (plan §15l).
#
# Why a separate database. van.db belongs to the poller and van-data.service reads it with
# ReadOnlyPaths=/srv/van/data on purpose, so this server cannot write a mark into it - and should
# not be able to. The marks are the server's own state, so they live with the other writable state
# in var/, next to dismissed.json. The GPS track stays in van.db and is read here read-only.
PLACES_DB = "/home/ada/van/var/places.db"

# Matt's threshold, 06-Nov-2026: 3/4 mile. A name that already exists but whose fix is further than
# this from every visit to that place is an ERROR, not a new place - "same name, different town" is
# the mistake this exists to catch.
PLACE_RADIUS_M = 1207.0

PLACES_SCHEMA = """
CREATE TABLE IF NOT EXISTS place (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT UNIQUE NOT NULL,
    first_ts TEXT,
    last_ts  TEXT
);
CREATE TABLE IF NOT EXISTS visit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT,
    place_id INTEGER,
    lat      REAL,
    lon      REAL,
    alt      REAL,
    sats     INTEGER,
    fix      INTEGER,
    source   TEXT
);
CREATE INDEX IF NOT EXISTS idx_visit_place ON visit(place_id);
CREATE TABLE IF NOT EXISTS survey (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT,
    place_id INTEGER,
    results  TEXT,
    run_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_survey_place ON survey(place_id);
"""

# Ada 06-Nov-2026 the survey table gained run_id after it already existed in the live file, so the
# column is added rather than assumed. The unique index is what makes filing a run idempotent: the
# result is read on every page poll, and only the first read may write it.
SURVEY_MIGRATIONS = (
    "ALTER TABLE survey ADD COLUMN run_id TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_survey_run ON survey(run_id)",
)


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Good enough at 1.2 km; the earth is not a sphere."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def places_con(path=None):
    con = sqlite3.connect(path or PLACES_DB, timeout=10)
    con.executescript(PLACES_SCHEMA)
    for stmt in SURVEY_MIGRATIONS:
        try:
            con.execute(stmt)
        except sqlite3.Error:
            pass  # already there
    con.commit()
    return con


def latest_fix(db=None):
    """The newest GPS sample, or None.

    Read-only on purpose: van.db is the poller's file. A missing table (before the first drain) is
    "no fix", not an error - the page has to be able to say "no fix yet" without a 500.
    """
    try:
        con = sqlite3.connect("file:%s?mode=ro" % (db or DB), uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT ts, lat, lon, alt, sats, fix FROM gps ORDER BY ts DESC LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or row[1] is None or row[2] is None:
        return None
    return {"ts": row[0], "lat": row[1], "lon": row[2], "alt": row[3], "sats": row[4], "fix": row[5]}


def place_mark(name, now=None, db=None, places_db=None, source="mark"):
    """(http status, body) for POST /place/mark.

    The rules, all of them Matt's (06-Nov-2026):
      * no fix, no mark - a null position is worse than a gap;
      * an existing name within 3/4 mile of any of its visits is the same place, and this is a visit;
      * an existing name further away is an ERROR, and the error says how far and when;
      * a new name that lands within 3/4 mile of a different place is an ERROR too (Matt,
        09-Nov-2026): it used to be created with a warning, and a warning stops nobody - the first
        near-duplicate on record, "Bob", landed 2 m from "Ada's Home".
    """
    name = (name or "").strip()
    if not name:
        return 400, {"error": "name required"}
    if len(name) > 64:
        return 400, {"error": "name too long (64 characters max)"}

    fix = latest_fix(db)
    if not fix:
        return 409, {"error": "no fix",
                     "detail": "Delilah has no GPS fix right now, so there is no position to mark. "
                               "A cold start takes a few minutes; indoors it may never fix."}

    con = places_con(places_db)
    try:
        row = con.execute("SELECT id, name FROM place WHERE name = ? COLLATE NOCASE",
                          (name,)).fetchone()
        if row:
            place_id, stored = row
            visits = con.execute(
                "SELECT ts, lat, lon FROM visit WHERE place_id = ?", (place_id,)).fetchall()
            nearest = None
            for ts, lat, lon in visits:
                if lat is None or lon is None:
                    continue
                d = haversine_m(fix["lat"], fix["lon"], lat, lon)
                if nearest is None or d < nearest[0]:
                    nearest = (d, ts, lat, lon)
            if nearest and nearest[0] > PLACE_RADIUS_M:
                return 409, {
                    "error": "too far",
                    "name": stored,
                    "distance_m": round(nearest[0]),
                    "radius_m": PLACE_RADIUS_M,
                    "nearest_ts": nearest[1],
                    "nearest_lat": nearest[2],
                    "nearest_lon": nearest[3],
                    "detail": "\"%s\" is already a place %.1f mi from here. Use a different name, "
                              "or mark it where it was." % (stored, nearest[0] / 1609.344),
                }
            con.execute(
                "INSERT INTO visit (ts, place_id, lat, lon, alt, sats, fix, source)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (fix["ts"], place_id, fix["lat"], fix["lon"], fix["alt"], fix["sats"],
                 fix["fix"], source))
            con.execute("UPDATE place SET last_ts = ? WHERE id = ?", (fix["ts"], place_id))
            con.commit()
            return 200, {"ok": True, "place": {"id": place_id, "name": stored},
                         "visit_ts": fix["ts"], "distance_m": round(nearest[0]) if nearest else None,
                         "new_place": False}

        # A new name standing on top of an existing place is REFUSED (Matt, 09-Nov-2026). It used
        # to be created with a warning; the rule is now the far-away rule read the other way round,
        # because "same place, different name" makes the same mess as "same name, different place".
        # Nearest wins, so the detail names the place the user probably meant.
        # Ada 10-Nov-2026 deployed: this half was committed on ada/place-radius-both-ways but absent
        # from the live tree, which the services execute - see board/geddy's rev- on the refusal.
        nearest = None
        for pid, pname, ts, lat, lon in con.execute(
                "SELECT p.id, p.name, v.ts, v.lat, v.lon FROM place p"
                " JOIN visit v ON v.place_id = p.id"):
            if lat is None or lon is None:
                continue
            d = haversine_m(fix["lat"], fix["lon"], lat, lon)
            if nearest is None or d < nearest[0]:
                nearest = (d, pname, pid, ts)
        if nearest and nearest[0] <= PLACE_RADIUS_M:
            # Metres, not "0.0 mi": the near-duplicate that started this was two metres away.
            away = (("%.0f m" % nearest[0]) if nearest[0] < 161.0
                    else ("%.1f mi" % (nearest[0] / 1609.344)))
            return 409, {
                "error": "too close",
                "name": name,
                "existing": nearest[1],
                "existing_id": nearest[2],
                "distance_m": round(nearest[0]),
                "radius_m": PLACE_RADIUS_M,
                "nearest_ts": nearest[3],
                "detail": "\"%s\" is already a place %s from here. Use that name, or mark "
                          "this one where it belongs." % (nearest[1], away),
            }
        cur = con.execute("INSERT INTO place (name, first_ts, last_ts) VALUES (?,?,?)",
                          (name, fix["ts"], fix["ts"]))
        place_id = cur.lastrowid
        con.execute(
            "INSERT INTO visit (ts, place_id, lat, lon, alt, sats, fix, source)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (fix["ts"], place_id, fix["lat"], fix["lon"], fix["alt"], fix["sats"], fix["fix"], source))
        con.commit()
        return 201, {"ok": True, "place": {"id": place_id, "name": name},
                     "visit_ts": fix["ts"], "new_place": True}
    finally:
        con.close()


# Ada 06-Nov-2026 the WAN survey (plan §15j). The engine runs on Delilah, detached, because its
# last step can drop the link that started it; this server only starts it and reads the answer.
SURVEY_LAST = "/overlay/survey/last.json"
SURVEY_SCRIPT = "/overlay/survey/wan-survey.py"
SURVEY_CACHE = {"at": 0.0, "value": None}


def _delilah(cmd, timeout=20):
    """Run one command on Delilah.

    -F /dev/null on purpose: this service runs in a systemd mount namespace (ReadOnlyPaths on
    /srv/van/data), and in that namespace ssh refuses the host's own /etc/ssh/ssh_config.d include
    with "Bad owner or permissions" even though the file is root:root 644. The include only sets
    keepalive options, which a 15-second control command does not need, so the system config is
    skipped rather than fought with.
    """
    return subprocess.run(["ssh", "-F", "/dev/null", "-o", "BatchMode=yes",
                           "-o", "ConnectTimeout=8", "root@delilah", cmd],
                          capture_output=True, text=True, timeout=timeout)


def ensure_place(name, places_db=None):
    """The id for `name`, creating the place if it is new. No visit: a survey needs no fix."""
    con = places_con(places_db)
    try:
        row = con.execute("SELECT id FROM place WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        if row:
            return row[0]
        cur = con.execute("INSERT INTO place (name, first_ts, last_ts) VALUES (?,?,?)",
                          (name, None, None))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def start_survey(name, places_db=None, db=None):
    """(status, body) for POST /survey/run: file the place, then launch the engine detached."""
    name = (name or "").strip()
    if not name:
        return 400, {"error": "name required"}
    if len(name) > 64:
        return 400, {"error": "name too long (64 characters max)"}

    # The place exists from the moment the survey is asked for. A survey does not need a fix, so a
    # missing one is not a reason to refuse - but a name that belongs to a place far away, or a new
    # name standing on top of one, is. Both are place_mark's refusals and both come back to the page
    # untouched: ensure_place() below must not quietly create the place a refusal just rejected.
    status, body = place_mark(name, db=db, places_db=places_db, source="survey")
    if status == 409 and body.get("error") in ("too far", "too close"):
        return status, body
    if status not in (200, 201):
        ensure_place(name, places_db)

    run_id = "%d" % int(time.time())
    # start-stop-daemon, not "setsid nohup ... &": this router has neither setsid nor nohup, and a
    # backgrounded command in a login shell returns 0 whether or not it started - which is how a
    # survey that never ran looked like a survey that had. -b detaches, -m writes the pidfile, and
    # the exit status is the launcher's, so a failure is a failure.
    cmd = ("rm -f /tmp/wan-survey.pid; start-stop-daemon -S -b -m -p /tmp/wan-survey.pid "
           "-x /usr/bin/python3 -- %s %s %s"
           % (SURVEY_SCRIPT, shlex.quote(name), shlex.quote(run_id)))
    try:
        r = _delilah(cmd, timeout=25)
    except Exception as e:
        return 503, {"error": "cannot reach Delilah: %s" % e}
    if r.returncode != 0:
        return 503, {"error": "cannot start the survey: %s" % (r.stderr or "").strip()[:200]}
    SURVEY_CACHE.update({"at": 0.0, "value": None})
    return 202, {"ok": True, "run_id": run_id, "name": name, "state": "running"}


def _file_survey(run, places_db=None):
    """Store a finished run against its place. Idempotent: run_id is unique."""
    run_id = run.get("run_id")
    name = (run.get("name") or "").strip()
    if not run_id or not name:
        return
    try:
        con = places_con(places_db)
        try:
            row = con.execute("SELECT id FROM place WHERE name = ? COLLATE NOCASE",
                              (name,)).fetchone()
            con.execute("INSERT OR IGNORE INTO survey (ts, place_id, results, run_id)"
                        " VALUES (?,?,?,?)",
                        (run.get("finished") or run.get("updated"),
                         row[0] if row else None, json.dumps(run), run_id))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def survey_state(max_age=5.0, places_db=None):
    """The latest run, read from Delilah. Cached briefly: the page polls every 15s.

    A read that fails keeps the last answer and marks it stale, because during a survey the router
    is expected to be unreachable for a moment - that is the switch, not a fault.
    """
    now = time.time()
    if max_age and SURVEY_CACHE["value"] is not None and now - SURVEY_CACHE["at"] < max_age:
        return SURVEY_CACHE["value"]
    try:
        r = _delilah("cat " + SURVEY_LAST, timeout=15)
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError((r.stderr or "no output").strip()[:200])
        value = json.loads(r.stdout)
    except Exception as e:
        prev = SURVEY_CACHE["value"]
        value = dict(prev) if prev else {"state": "unknown"}
        value["stale"] = True
        value["error"] = str(e)[:200]
    if value.get("state") == "done":
        _file_survey(value, places_db)
    SURVEY_CACHE.update({"at": now, "value": value})
    return value


# Ada 06-Nov-2026 the GPS track for the history page (plan §15k/§15l). A separate reader from
# read_history() because the gps table's ts is the GPS's own ISO-8601 UTC string, not the epoch
# float every other table uses - the whole point of it being the fix's clock rather than the
# router's. Comparing it to a float would silently return nothing.
def read_track(hours=None, db=None):
    """Columnar track, oldest first. `hours` falsy (None or 0) means everything we hold.

    Ada 09-Nov-2026 Matt: "the whole trip's track rather than the last day". A track is a SHAPE --
    "where have we been" is cumulative -- so the position panel asks for the whole record while the
    charts beside it keep their own window. Two things follow from letting the query be unbounded:
    the response must still be bounded (MAX_POINTS, the same cap the charts use), and the LAST fix
    must survive the thinning, because the panel draws it as the amber "you are here" pin and a
    strided last point would put that pin up to `stride` intervals in the past.
    """
    db = DB if db is None else db
    if not os.path.exists(db):
        return {"error": "no history database yet"}
    where, args = "", ()
    if hours:
        since = time.time() - hours * 3600
        since_iso = datetime.datetime.utcfromtimestamp(since).strftime("%Y-%m-%dT%H:%M:%SZ")
        where, args = " WHERE ts >= ?", (since_iso,)
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        try:
            rows = con.execute(
                "SELECT ts, lat, lon, alt, speed, sats FROM gps" + where + " ORDER BY ts ASC",
                args).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        return {"error": "cannot read the track: %s" % e}

    total = len(rows)
    stride = max(1, int(total / MAX_POINTS) + 1)
    if stride > 1:
        newest = rows[-1]
        rows = rows[::stride]
        if rows[-1] is not newest:
            rows.append(newest)      # the pin marks the newest fix, not the nearest strided one

    out = {"hours": hours, "count": 0, "total": total, "stride": stride,
           "ts": [], "lat": [], "lon": [], "alt": [], "speed": [], "sats": []}
    for ts, lat, lon, alt, speed, sats in rows:
        try:
            epoch = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except (TypeError, ValueError):
            continue
        out["ts"].append(epoch)
        out["lat"].append(lat)
        out["lon"].append(lon)
        out["alt"].append(alt)
        out["speed"].append(speed)
        out["sats"].append(sats)
    out["count"] = len(out["ts"])
    return out


# Ada 06-Nov-2026 the survey history for the report page (plan §15j). The runs are filed in
# places.db as they finish, so this is a read of what already happened rather than a live query.
def read_surveys(limit=20, places_db=None):
    try:
        con = places_con(places_db)
        try:
            rows = con.execute(
                "SELECT s.id, s.ts, s.run_id, p.name, s.results FROM survey s"
                " LEFT JOIN place p ON p.id = s.place_id ORDER BY s.id DESC LIMIT ?",
                (limit,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        return {"error": "cannot read surveys: %s" % e}
    out = []
    for sid, ts, run_id, name, results in rows:
        try:
            d = json.loads(results or "{}")
        except ValueError:
            d = {}
        out.append({"id": sid, "ts": ts, "run_id": run_id, "name": name,
                    "winner": d.get("winner_label"), "state": d.get("state"),
                    "results": d.get("results") or [], "swept": d.get("swept") or [],
                    "switch": d.get("switch") or {}, "cost_bytes": d.get("cost_bytes"),
                    "configured": d.get("configured") or [], "visible": d.get("visible") or []})
    return {"surveys": out}


def read_places(places_db=None, db=None):
    """Every place with its visit and survey counts, newest visit first."""
    try:
        con = places_con(places_db)
    except sqlite3.Error as e:
        return {"error": "cannot open places: %s" % e}
    try:
        rows = con.execute(
            "SELECT p.id, p.name, p.first_ts, p.last_ts,"
            " (SELECT COUNT(*) FROM visit v WHERE v.place_id = p.id),"
            " (SELECT COUNT(*) FROM survey s WHERE s.place_id = p.id),"
            " (SELECT v.lat FROM visit v WHERE v.place_id = p.id ORDER BY v.ts DESC LIMIT 1),"
            " (SELECT v.lon FROM visit v WHERE v.place_id = p.id ORDER BY v.ts DESC LIMIT 1)"
            " FROM place p ORDER BY p.last_ts DESC").fetchall()
    finally:
        con.close()
    return {
        "radius_m": PLACE_RADIUS_M,
        "latest_fix": latest_fix(db),
        "places": [{"id": r[0], "name": r[1], "first_ts": r[2], "last_ts": r[3],
                    "visits": r[4], "surveys": r[5], "lat": r[6], "lon": r[7]} for r in rows],
    }



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


# Ada 06-Nov-2026 workstream D: the router's own history, same columnar contract as the dish.
# The dish table is all numbers; the router's carries state words too (active path, SSID, APN),
# which must come back as text rather than being forced through float().
FIELDS_ROUTER = {c: c for c in (
    "signal_dbm", "noise_dbm", "bitrate", "link_quality", "channel", "wwan_uptime",
    "kmwan_level", "kmwan_metric", "csq", "temp_c", "load1", "mem_avail", "mem_total",
    "uptime", "rx_bytes", "tx_bytes", "sim_slot", "traffic_sim1", "traffic_sim2", "cell_ok",
)}
FIELDS_ROUTER_TEXT = {c: c for c in ("ssid", "kmwan_active", "qcsq", "cereg", "cops", "apn")}
HISTORY_TABLES = {
    "dish": {"fields": FIELDS, "text": set()},
    "router": {"fields": dict(FIELDS_ROUTER, **FIELDS_ROUTER_TEXT),
               "text": set(FIELDS_ROUTER_TEXT)},
}


def read_history(hours, fields, table="dish", db=None):
    """Columnar history from van.db. Returns (payload_dict, error_str)."""
    db = DB if db is None else db
    spec = HISTORY_TABLES.get(table)
    if spec is None:
        return None, "unknown table %r (have: %s)" % (table, ", ".join(sorted(HISTORY_TABLES)))
    if not os.path.exists(db):
        return None, "no history database yet"
    want = [f for f in fields if f in spec["fields"]]
    if not want:
        return None, "no usable fields requested for %s: field not stored" % table

    cols = ", ".join(["ts"] + [spec["fields"][f] for f in want])
    since = time.time() - hours * 3600

    # mode=ro: never create or write the poller's file. uri=True is required for
    # the query-string form; timeout lets a poll in flight finish first.
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    except sqlite3.Error as e:
        return None, "cannot open history: %s" % e
    try:
        rows = con.execute(
            "SELECT %s FROM %s WHERE ts >= ? ORDER BY ts ASC" % (cols, table), (since,)
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
        if f in spec["text"]:
            out[f] = [r[i + 1] for r in rows]
        else:
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

# Ada 10-Nov-2026 the second manual figure: EvilDish's own cycle up to the tick this box first
# identified it. Matt, 10-Nov-2026: "I show 51Gb for EvilDish in the current billing cycle" - the
# Starlink app is the authority for a billed figure, and it is the only source for the part of the
# cycle this box never read: until 11:46 the van's kit was off the WAN jack and another terminal
# (BigBoyDish) answered 192.168.100.1, and before 06-Nov no row carried an identity at all.
# The arithmetic, so nobody has to re-derive it:
#     51.00 GB  (Starlink, 10-Nov-2026 ~12:45 EST)
#   -  0.251 GB  what this file counted for the cycle by itself (EvilDish's own increase from 11:46)
#   = 50.75 GB  the part it cannot measure, which is what the constant below holds.
# The window's END is that identification tick: rows after it are counted, so nothing is plugged
# twice. Legacy rows inside the window stay UNCOUNTED - a firmware string is not an identity (the
# Mini's `cr88921.3` rows of 30-Oct..05-Nov are almost certainly EvilDish's, but another kit can
# report the same string) - so if one is ever added to VAN_DISH_IDS, this total must shrink by
# exactly those bytes. The split is EvilDish's own measured down/up shape (80/20, from its rows in
# the store); it is cosmetic here, because the tile shows the service total.
EVILDISH_PLUG_WINDOW = ("2026-10-25", "2026-11-10T11:46:08")   # UTC, [start, end)
EVILDISH_PLUG_TOTAL = 50.75e9
EVILDISH_PLUG_SPLIT = (0.80, 0.20)   # down, up

# Ada 10-Nov-2026 both figures in one place, so what is plugged and why is one read. Each applies
# only to a period that contains its own window, which is what keeps a closed cycle's missing bytes
# out of the current one. Plan §16/T11 would replace this with coverage reporting; until then a
# figure here is scoped to its window and named in /usage's `manual_lump` so the page never has to
# guess what the total is made of.
LUMPS = (
    {"window": MANUAL_WINDOW, "total": MANUAL_TOTAL, "split": MANUAL_SPLIT,
     "label": "Oct 21-24 roaming (no rows: the store's first dish sample is 20-Oct)"},
    {"window": EVILDISH_PLUG_WINDOW, "total": EVILDISH_PLUG_TOTAL, "split": EVILDISH_PLUG_SPLIT,
     "label": "EvilDish's own cycle up to the identification tick (Starlink's figure)"},
)

# Ada 06-Nov-2026 the van's own dish, and the only terminal /van may count.
# Matt: "ignore data from BigBoyDish, and do not show BigBoyDish data on the Van Page." Two
# Starlink terminals answer the same address, and this box has spent the cycle reading Geddy's
# kit over the home path while the van's dish sat powered off on the WAN jack. Their counters
# have unrelated baselines, so one total across both is mostly someone else's usage.
# Entries are matched against terminal_group(): the stored dish_id, or the firmware string for
# rows written before the poller stored one. EMPTY MEANT NOTHING WAS ATTRIBUTED - the honest state
# until EvilDish is up and its identity is confirmed. Ada 10-Nov-2026 it is confirmed and listed
# below, so the dish half of the usage tile counts the van's kit only. Legacy rows with no
# dish_id group by firmware string, which cannot match an id, so they stay uncounted rather than
# being attributed to a kit nobody recorded - expect the cycle figure to start small (it counts
# EvilDish's own samples from 11:46 on 10-Nov) instead of borrowing the home kit's total.
VAN_DISH_IDS = ("ut41c81581-84505c0f-dac5a295",)   # EvilDish, read off the WAN jack 10-Nov-2026


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


def sim_usage(con):
    """Latest SIM-meter row per slot: what the router has seen since the cycle began.

    Ada 06-Nov-2026 Task 4's meter writes a row per SIM per poll, rolling the counter's peak
    forward across a module re-init, so the newest row per slot carries the cycle figure the
    page shows for SIM2. Absent table, absent data: a store from before the meter simply
    reports nothing rather than failing the whole usage response.
    """
    if not {r[1] for r in con.execute("PRAGMA table_info(sim)")}:
        return {}
    out = {}
    for ts, slot, cs, pb, cb, tb, cap in con.execute(
            "SELECT ts, slot, cycle_start, period_bytes, carried_bytes, total_bytes,"
            " cap_bytes FROM sim ORDER BY ts ASC"):
        out[str(slot)] = {"ts": ts, "cycle_start": cs, "period_bytes": pb,
                          "carried_bytes": cb, "total_bytes": tb, "cap_bytes": cap}
    return out


def _window_bounds(window):
    """(start, end) epoch seconds of a LUMPS window, each bound a date or a date-time."""
    import datetime as _dt
    out = []
    for s in window:
        fmt = "%Y-%m-%dT%H:%M:%S" if "T" in s else "%Y-%m-%d"
        out.append(_dt.datetime.strptime(s, fmt).replace(
            tzinfo=_dt.timezone.utc).timestamp())
    return out[0], out[1]


def lumps_for(start, end):
    """(down, up, detail) of every manual figure this period contains, else (0, 0, []).

    `start`/`end` are period bounds as epoch seconds (UTC), inclusive end. A window applies only
    when the period overlaps it, which is what scoped the Oct 21-24 figure to its own cycle.
    """
    down = up = 0.0
    detail = []
    for lump in LUMPS:
        w0, w1 = _window_bounds(lump["window"])
        if end < w0 or start >= w1:
            continue
        d = lump["total"] * lump["split"][0]
        u = lump["total"] * lump["split"][1]
        down += d
        up += u
        detail.append({"window": "%s..%s" % (lump["window"][0], lump["window"][1]),
                       "label": lump["label"], "total": lump["total"], "down": d, "up": u})
    return down, up, detail


def manual_lump_for(start, end):
    """(down, up) of the un-covered Oct 21-24 roaming, or (0.0, 0.0) if absent.

    `start`/`end` are period bounds as epoch seconds (UTC), inclusive end.
    Ada 10-Nov-2026 now the sum over LUMPS (that figure plus EvilDish's own cycle plug); the
    name is kept because it is the documented entry point for a period's manual figures.
    """
    down, up, _ = lumps_for(start, end)
    return (down, up)


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


def read_usage(db=None, van_ids=None):
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
    if van_ids is None:
        van_ids = VAN_DISH_IDS
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
        sims = sim_usage(con)
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

    # Ada 06-Nov-2026 count the van's kit only, and say so when there is nothing to count.
    van_set = {str(v) for v in van_ids}
    for key, g in groups.items():
        g["van"] = key in van_set
    counted = [g for g in groups.values() if g["van"]]
    if not van_set:
        attribution = {
            "van_dish_ids": [],
            "counted": [],
            "reason": "no van-dish identity is configured (VAN_DISH_IDS is empty), so no "
                      "terminal's usage can be attributed to the van's dish",
        }
        down = up = None
    elif not counted:
        attribution = {
            "van_dish_ids": sorted(van_set),
            "counted": [],
            "reason": "no dish rows in this period belong to the van's kit",
        }
        down = up = None
    else:
        down = sum(g["down"] for g in counted)
        up = sum(g["up"] for g in counted)
        attribution = {"van_dish_ids": sorted(van_set),
                       "counted": [g["label"] for g in counted],
                       "reason": "van's kit only"}
    # the breakdown stays in the response as diagnostics, flagged, so the page can decide
    # what to render instead of having the decision buried in here
    terminals = [{k: g[k] for k in ("id", "hw", "label", "van", "down", "up",
                                    "samples", "first", "last")}
                 for g in sorted(groups.values(), key=lambda x: x["down"], reverse=True)]

    now = time.time()
    # Ada 06-Nov-2026 the lump, only for a period that contains Oct 21-24 (see MANUAL_WINDOW).
    # Ada 10-Nov-2026 the same call now also carries EvilDish's own cycle up to its identification
    # tick (see EVILDISH_PLUG_WINDOW), so the dish total matches Starlink's own figure for the kit.
    manual_down, manual_up, manual_detail = lumps_for(start, end)
    if down is None:
        manual_down = manual_up = 0.0        # an unknown total has nothing to add it to
    else:
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
        "total": None if down is None else down + up,
        # Ada 06-Nov-2026 what the totals are made of, and why they can be null.
        "attribution": attribution,
        # Ada 06-Nov-2026 the other metered service, so the tile reads one endpoint: SIM2's
        # cycle figure as the router's own meter recorded it (the carrier's portal governs).
        "sim": sims,
        # The database only started collecting on 22-Oct-2026, so the period is
        # covered from then -- the page says so rather than claiming a full month.
        "covered_from": rows[0][0],
        "covered_to": rows[-1][0],
        "samples": len(rows),
        # Ada 06-Nov-2026 per-terminal detail: the total is the sum of these, so the page can
        # name which terminal it is reading instead of silently using both.
        "terminals": terminals,
        # Ada 06-Nov-2026 say whether the manual figures were counted, and how much of each,
        # so nobody has to read this file to know what the total is made of.
        # Ada 10-Nov-2026 there can now be two (the Oct 21-24 roaming figure and EvilDish's own
        # cycle plug), so the detail is a list and `windows` names them all.
        "manual_lump": {
            "applied": bool(manual_down or manual_up),
            "down": manual_down,
            "up": manual_up,
            "window": "; ".join(d["window"] for d in manual_detail),
            "windows": [d["window"] for d in manual_detail],
            "lumps": manual_detail,
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

        # Ada 06-Nov-2026 Mark Location (plan §15l). The name is the join key between a placemark
        # and a survey, so this is the cheap half of the pair: no disruption, no data, and it works
        # whether or not the dish is up.
        if path == "/place/mark":
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
                status, body = place_mark(payload.get("name"))
            except sqlite3.Error as e:
                self._respond(500, "application/json; charset=utf-8",
                              json.dumps({"error": "cannot write places: %s" % e}).encode("utf-8"))
                return
            self.no_store = True
            self._respond(status, "application/json; charset=utf-8",
                          json.dumps(body).encode("utf-8"))
            return

        # Ada 06-Nov-2026 start a WAN survey (plan §15j). Manual only: nothing here runs on a timer,
        # and the engine is launched detached because its last step can drop the link.
        if path == "/survey/run":
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
                status, body = start_survey(payload.get("name"))
            except sqlite3.Error as e:
                self._respond(500, "application/json; charset=utf-8",
                              json.dumps({"error": "cannot write places: %s" % e}).encode("utf-8"))
                return
            self.no_store = True
            self._respond(status, "application/json; charset=utf-8",
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

            table = (q.get("table", ["dish"])[0] or "dish").strip()
            payload, err = read_history(hours, fields, table=table)
            if err:
                self._respond(404, "application/json; charset=utf-8",
                              json.dumps({"error": err, "hours": hours}).encode("utf-8"))
            else:
                self._respond(200, "application/json; charset=utf-8",
                              json.dumps(payload).encode("utf-8"))
            return

        if u.path.rstrip("/") in ("/surveys", "/surveys.json"):
            # Ada 06-Nov-2026 the survey history (plan §15j).
            q = urllib.parse.parse_qs(u.query)
            try:
                limit = int(q.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(200, limit))
            self.no_store = True
            self._respond(200, "application/json; charset=utf-8",
                          json.dumps(read_surveys(limit)).encode("utf-8"))
            return

        if u.path.rstrip("/") in ("/track", "/track.json"):
            # Ada 06-Nov-2026 the GPS track for the history page (plan §15k).
            # Ada 09-Nov-2026 `hours=0` means the whole record (Matt: "the whole trip's track
            # rather than the last day"). The DEFAULT stays bounded on purpose: an endpoint that
            # hands back everything when asked nothing is a trap on a record that only grows, so
            # the page asks for the whole trip explicitly and a stray caller does not.
            q = urllib.parse.parse_qs(u.query)
            try:
                hours = float(q.get("hours", ["24"])[0])
            except ValueError:
                hours = 24.0
            hours = None if hours <= 0 else max(0.25, min(24 * 90, hours))
            self.no_store = True
            self._respond(200, "application/json; charset=utf-8",
                          json.dumps(read_track(hours)).encode("utf-8"))
            return

        if u.path.rstrip("/") in ("/survey", "/survey.json"):
            # Ada 06-Nov-2026 the latest survey run, live from Delilah (plan §15j).
            self.no_store = True
            self._respond(200, "application/json; charset=utf-8",
                          json.dumps(survey_state()).encode("utf-8"))
            return

        if u.path.rstrip("/") in ("/places", "/places.json"):
            # Ada 06-Nov-2026 the place registry, plus the fix a mark would use right now, so the
            # page can say "no fix" before the button is pressed rather than after (plan §15l).
            self.no_store = True
            self._respond(200, "application/json; charset=utf-8",
                          json.dumps(read_places()).encode("utf-8"))
            return

        if u.path.rstrip("/") in ("/version", "/version.json"):
            # Ada 06-Nov-2026 which build of the page is this (Matt's footer)
            self._respond(200, "application/json; charset=utf-8",
                          json.dumps(page_version()).encode("utf-8"))
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

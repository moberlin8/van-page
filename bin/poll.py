#!/usr/bin/env python3
"""## Ada 22-Oct-2026 poll the Starlink Mini (via the Delilah ssh tunnel) and the
## Delilah router itself, storing everything in one sqlite file on harlan.

Run by /home/ada/van/bin/poll-forever.sh every 60s. Each tick:
  * writes one row to van_data (dish snapshot, latest-wins semantics)
  * appends to history_* for anything that must persist across restarts
  * refreshes data/snapshot.json, the single file the page reads

Design notes:
  * The dish is reached through the ssh tunnel harlan:19200 -> delilah ->
    192.168.100.1:9200. If the tunnel is down the poll FAILS SOFT: the last
    good row is kept and snapshot.json gets "stale": true. The page renders the
    last reading with a stale marker rather than a blank tile.
  * Delilah is polled over ssh each tick (~2s). Failure there is the same:
    soft, and the last good Delilah reading survives in the file.
  * Nothing is written anywhere but this directory.
  * Ada 27-Oct-2026 records notable state changes (SSID, failover, WAN, cell,
    SIM, dish stale/recovery) as an events array in snapshot.json for the page.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# /srv/van/data is what the homepage container serves at /van/data/
# (ROOT/data would be a second copy that nothing reads).
DATA = "/srv/van/data"
VAR = os.path.join(ROOT, "var")
DB = os.path.join(DATA, "van.db")
SNAPSHOT = os.path.join(DATA, "snapshot.json")

GRPC_TOOLS = "/home/ada/starlink/starlink-grpc-tools"
VENV = "/home/ada/starlink/.venv/bin/python3"
DISH_TARGET = "127.0.0.1:19200"

# Ada 24-Oct-2026 GL.iNet's own firmware-update check (Matt). The router's web UI
# asks https://firmware-api.gl-inet.com/api/firmware/check whether a newer build
# exists for this model, and the page shows the answer. It is a third-party call,
# so it runs on its own clock (every 6h) and its result is cached in a file: a
# failure keeps the last answer instead of blanking the indicator.
FW_API = "https://firmware-api.gl-inet.com/api/firmware/check"
FW_STATE = os.path.join(DATA, "fw-update.json")
FW_INTERVAL = 6 * 3600
FW_DOWNLOAD = "https://dl.gl-inet.com/router/"


def check_firmware(delilah, now):
    """Return the cached update check, refreshing it when it is due."""
    state = None
    try:
        with open(FW_STATE) as f:
            state = json.load(f)
    except Exception:
        state = None

    if not state or now - (state.get("checked_at") or 0) > FW_INTERVAL:
        fresh = None
        try:
            body = json.dumps({
                "timezone": delilah.get("zonename") or "UTC",
                "country_code": (delilah.get("country_code") or "us").lower(),
                "language": "en",
                "build_number": str(delilah.get("fw_build") or ""),
                "model": delilah.get("hw_model") or "",
                "version": delilah.get("router_fw_ver") or "",
            }).encode("utf-8")
            req = urllib.request.Request(
                FW_API, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                out = json.loads(r.read().decode("utf-8"))
            # NOTE the API sends upgrade_available as a STRING ("false"/"true"),
            # so bool() on it would read "false" as True.
            fresh = {
                "checked_at": now,
                "available": str(out.get("upgrade_available", "")).lower() == "true",
                "new_version": out.get("new_version"),
                "url": FW_DOWNLOAD + (delilah.get("hw_model") or "x3000"),
                "error": None,
            }
            log("fw check ok  available=%s new=%s" % (
                fresh["available"], fresh["new_version"]))
        except Exception as e:
            log("fw check FAILED: %s" % e)
            fresh = None
        if fresh is not None:
            state = fresh
        elif state is None:
            # never had an answer; say so rather than showing "current"
            state = {"checked_at": None, "available": None,
                     "new_version": None, "url": None, "error": "no answer yet"}
        else:
            state = dict(state, error="refresh failed")
        try:
            tmp = FW_STATE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, FW_STATE)
        except Exception:
            pass
    return state

DELILAH_HOST = "root@delilah"
DELILAH_SCRIPT = "/tmp/van-probe.sh"
DELILAH_TIMEOUT = 45

SCHEMA = """
CREATE TABLE IF NOT EXISTS dish (
    ts            REAL PRIMARY KEY,
    state         TEXT,
    sw_version    TEXT,
    uptime_s      REAL,
    ping_drop     REAL,
    ping_latency  REAL,
    downlink_bps  REAL,
    uplink_bps    REAL,
    snr           REAL,
    frac_obstruct REAL,
    obstructed    INTEGER,
    azimuth       REAL,
    elevation     REAL,
    gps_sats      INTEGER,
    alerts        INTEGER,
    samples_15m   INTEGER,
    ping_drop_15m REAL,
    latency_mean  REAL,
    latency_p90   REAL,
    usage_down    INTEGER,
    usage_up      INTEGER,
    power_w       REAL
);
CREATE INDEX IF NOT EXISTS idx_dish_ts ON dish(ts);
"""

# Ada 23-Oct-2026 the dish reports two rolling averages of its "prolonged"
# obstruction events. They are not in the table as originally written, so they
# are added if missing -- the table may already exist with rows in it, which is
# why this is an ALTER rather than a recreate.
ADD_COLUMNS = (
    ("obstruction_duration", "REAL"),
    ("obstruction_interval", "REAL"),
    # Ada 23-Oct-2026 is_snr_above_noise_floor is a BOOLEAN, not a number, but
    # SQLite has no boolean type and the page only needs true/false/absent, so
    # INTEGER is the honest storage (1/0/NULL).
    ("is_snr_above_noise_floor", "INTEGER"),
)


def log(msg):
    sys.stdout.write("[poll] %s\n" % msg)
    sys.stdout.flush()


def http_json(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def poll_dish():
    """Return a dict of dish readings, or raise."""
    import sqlite3  # noqa: F401  (kept local so a missing stdlib fails loudly)

    code = r'''
import sys, json
sys.path.insert(0, %r)
import starlink_grpc as s
ctx = s.ChannelContext(%r)
status = s.status_data(context=ctx)
g, pdrop, pdrl, plat, llat, usage, power = s.history_stats(-1, context=ctx)

out = {
    "status": dict(status[0]),
    "obstruction": dict(status[1]),
    "alerts": dict(status[2]),
    "update": dict(status[3]),
    "general": g,
    "ping_drop": pdrop,
    "ping_latency": plat,
    "usage": usage,
    "power": power}
print(json.dumps(out))
''' % (GRPC_TOOLS, DISH_TARGET)

    r = subprocess.run([VENV, "-c", code], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("dish poll failed rc=%d: %s" % (r.returncode, r.stderr[-400:]))
    return json.loads(r.stdout.strip().splitlines()[-1])


def poll_delilah():
    """Return a dict of Delilah readings, or raise."""
    # pull the probe over first so the local copy is never stale
    subprocess.run(
        ["scp", "-O", "-q", "-o", "BatchMode=yes",
         os.path.join(ROOT, "bin", "delilah-probe.sh"),
         DELILAH_HOST + ":" + DELILAH_SCRIPT],
        capture_output=True, text=True, timeout=40,
    )
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         DELILAH_HOST, "sh " + DELILAH_SCRIPT],
        capture_output=True, text=True, timeout=DELILAH_TIMEOUT,
    )
    if r.returncode != 0:
        raise RuntimeError("delilah poll failed rc=%d: %s" % (r.returncode, r.stderr[-300:]))
    out = r.stdout.strip()
    start = out.find("{")
    if start < 0:
        raise RuntimeError("delilah poll produced no JSON")
    return json.loads(out[start:].splitlines()[0])


def num(v):
    """Coerce to float, or None. Handles the router's "999"/"0,5920" strings."""
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace('"', "")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def load_snapshot():
    try:
        with open(SNAPSHOT) as f:
            return json.load(f)
    except Exception:
        return {"dish": None, "delilah": None, "dish_ts": None, "delilah_ts": None}


def save_snapshot(snap):
    tmp = SNAPSHOT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, indent=1, sort_keys=True)
    os.replace(tmp, SNAPSHOT)


def main():
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(VAR, exist_ok=True)

    import sqlite3
    con = sqlite3.connect(DB, timeout=15)
    con.executescript(SCHEMA)
    # Ada 23-Oct-2026 migrate an existing table in place (see ADD_COLUMNS)
    for name, typ in ADD_COLUMNS:
        try:
            con.execute("ALTER TABLE dish ADD COLUMN %s %s" % (name, typ))
        except sqlite3.OperationalError:
            pass  # column already present on a re-run
    con.commit()

    snap = load_snapshot()
    now = time.time()

    # ---- state-change notifier (Matt, 27-Oct-2026) -------------------------------
    # snapshot.json is latest-wins, so we snapshot prev-state fields from the
    # loaded snap *before* overwriting them in this tick.
    prev_delilah = snap.get("delilah") or {}
    prev_dish_stale = snap.get("dish_stale", False)

    # ---- dish -----------------------------------------------------------------
    try:
        d = poll_dish()
        st = d.get("status", {})
        gen = d.get("general", {})
        pd = d.get("ping_drop", {})
        pl = d.get("ping_latency", {})
        us = d.get("usage", {})
        pw = d.get("power", {})

        dec = pl.get("deciles_all_ping_latency[]") or []
        p90 = dec[9] if len(dec) > 9 else None

        row = (
            now,
            st.get("state"),
            st.get("software_version"),
            num(st.get("uptime")),
            num(st.get("pop_ping_drop_rate")),
            num(st.get("pop_ping_latency_ms")),
            num(st.get("downlink_throughput_bps")),
            num(st.get("uplink_throughput_bps")),
            num(st.get("snr")),
            num(st.get("fraction_obstructed")),
            1 if st.get("currently_obstructed") else 0,
            num(st.get("direction_azimuth")),
            num(st.get("direction_elevation")),
            num(st.get("gps_sats")),
            num(st.get("alerts")),
            gen.get("samples"),
            num(pd.get("total_ping_drop")),
            num(pl.get("mean_all_ping_latency")),
            num(p90),
            us.get("download_usage"),
            us.get("upload_usage"),
            num(pw.get("latest_power")),
            # Ada 23-Oct-2026 rolling averages of "prolonged" obstruction events
            num(st.get("obstruction_duration")),
            num(st.get("obstruction_interval")),
            # Ada 23-Oct-2026 the SNR flag, stored as 1/0/NULL
            None if st.get("is_snr_above_noise_floor") is None
                else (1 if st.get("is_snr_above_noise_floor") else 0),
        )
        # Ada 23-Oct-2026 the column list is named explicitly: the ADD COLUMN
        # migrations above mean a bare VALUES (...) no longer matches the table.
        con.execute(
            "INSERT OR REPLACE INTO dish ("
            "ts, state, sw_version, uptime_s, ping_drop, ping_latency, "
            "downlink_bps, uplink_bps, snr, frac_obstruct, obstructed, "
            "azimuth, elevation, gps_sats, alerts, samples_15m, ping_drop_15m, "
            "latency_mean, latency_p90, usage_down, usage_up, power_w, "
            "obstruction_duration, obstruction_interval, is_snr_above_noise_floor"
            ") VALUES (%s)" % ", ".join("?" * len(row)), row)
        con.commit()
        snap["dish"] = {
            "ts": now,
            "state": st.get("state"),
            "sw_version": st.get("software_version"),
            "uptime_s": num(st.get("uptime")),
            "ping_drop": num(st.get("pop_ping_drop_rate")),
            "ping_latency": num(st.get("pop_ping_latency_ms")),
            "downlink_bps": num(st.get("downlink_throughput_bps")),
            "uplink_bps": num(st.get("uplink_throughput_bps")),
            "snr": num(st.get("snr")),
            "frac_obstruct": num(st.get("fraction_obstructed")),
            "obstructed": bool(st.get("currently_obstructed")),
            "azimuth": num(st.get("direction_azimuth")),
            "elevation": num(st.get("direction_elevation")),
            "gps_sats": num(st.get("gps_sats")),
            "alerts": (None if st.get("alerts") is None else int(num(st.get("alerts")))),
            "history_15m": {
                "samples": gen.get("samples"),
                "ping_drop": num(pd.get("total_ping_drop")),
                "latency_mean": num(pl.get("mean_all_ping_latency")),
                "latency_p90": num(p90),
            },
            "usage_15m": {
                "down": us.get("download_usage"),
                "up": us.get("upload_usage"),
            },
            "power_w": num(pw.get("latest_power")),
            # Ada 23-Oct-2026 avg consecutive prolonged obstruction, and avg time
            # between such events, in seconds
            "obstruction_duration": num(st.get("obstruction_duration")),
            "obstruction_interval": num(st.get("obstruction_interval")),
            # Ada 23-Oct-2026 the SNR flag: True/False, or null when the dish does
            # not report it. The page's connection light reads this -- keep the
            # distinction between false and null, or a missing measurement would
            # read as a fault.
            "is_snr_above_noise_floor": st.get("is_snr_above_noise_floor"),
        }
        snap["dish_ts"] = now
        snap["dish_stale"] = False
        snap.pop("dish_error", None)
        log("dish ok  state=%s lat=%s drop=%s" % (
            snap["dish"]["state"], snap["dish"]["ping_latency"], snap["dish"]["ping_drop"]))
    except Exception as e:
        snap["dish_stale"] = True
        snap["dish_error"] = str(e)[:300]
        # keep the previous dish/dish_ts so the page can still render last data;
        # do not advance dish_ts on a failed poll
        log("dish FAILED: %s" % e)

    # ---- delilah --------------------------------------------------------------
    try:
        r = poll_delilah()
        snap["delilah"] = r
        snap["delilah_ts"] = num(r.get("collected_at") and None) or now
        snap["delilah_stale"] = False
        snap.pop("delilah_error", None)
        log("delilah ok  wwan_up=%ss active=%s ssid=%s csq=%s" % (
            r.get("wwan_uptime"), r.get("kmwan_active"), r.get("ssid"), r.get("csq")))
    except Exception as e:
        snap["delilah_stale"] = True
        snap["delilah_error"] = str(e)[:300]
        log("delilah FAILED: %s" % e)

    # ---- firmware update check (Matt, 24-Oct-2026) ----------------------------
    # Attached to the router's own reading so the page can show it beside the
    # firmware row. Carries over unchanged when the router poll failed.
    if snap.get("delilah"):
        snap["delilah"]["fw_update"] = check_firmware(snap["delilah"], now)

    # ---- staleness markers ----------------------------------------------------
    snap["generated_at"] = now
    # staleness: treat an outstanding error as stale, and age out old data
    snap["dish_stale"] = bool(snap.get("dish_error")) or bool(snap.get("dish_ts") is None) or (
        now - (snap.get("dish_ts") or 0) > 180)
    if snap.get("dish_ts") is None:
        snap["dish_stale"] = True
    snap["delilah_stale"] = bool(snap.get("delilah_error")) or bool(snap.get("delilah_ts") is None) or (
        now - (snap.get("delilah_ts") or 0) > 180)
    if snap.get("delilah_ts") is None:
        snap["delilah_stale"] = True

    # ---- record state changes for the /van notifier (Matt, 27-Oct-2026) -----------
    events = list(snap.get("events", []))

    def _evt(msg):
        events.append({"ts": now, "msg": msg})

    nd = snap.get("delilah") or {}
    # WiFi SSID
    _o, _n = prev_delilah.get("ssid"), nd.get("ssid")
    if _o and _n and _o != _n:
        _evt("WiFi SSID changed from %s to %s" % (_o, _n))
    # kmwan failover (wan <-> wwan <-> other)
    _o, _n = prev_delilah.get("kmwan_active"), nd.get("kmwan_active")
    if _o and _n and _o != _n:
        _evt("Failover %s to %s" % (_o, _n))
    # WiFi WAN port up/down
    _o, _n = prev_delilah.get("kmwan_wan_up"), nd.get("kmwan_wan_up")
    if _o is not None and _n is not None and _o != _n:
        _evt("WAN Port %s" % ("up" if _n else "down"))
    # cellular health
    _o, _n = prev_delilah.get("cell_ok"), nd.get("cell_ok")
    if _o is not None and _n is not None and _o != _n:
        _evt("Cellular %s" % ("active" if _n else "inactive"))
    # SIM swap (IMSI)
    _o, _n = prev_delilah.get("imsi"), nd.get("imsi")
    if _o and _n and _o != _n:
        _evt("SIM changed (IMSI %s -> %s)" % (_o, _n))
    # dish stale / recovered
    if not prev_dish_stale and snap.get("dish_stale"):
        _evt("Dish data became stale")
    elif prev_dish_stale and not snap.get("dish_stale"):
        _evt("Dish data recovered")

    snap["events"] = events[-20:]  # cap at 20 most-recent
    save_snapshot(snap)
    con.close()


if __name__ == "__main__":
    main()

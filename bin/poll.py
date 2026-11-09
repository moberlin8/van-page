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
  * Ada 02-Nov-2026 the events now carry a stable id and age out on a 26h window
    instead of a 20-entry cap, so the page can keep them 24h and dismiss them.
"""
import json
import os
import shlex
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
    -- Ada 06-Nov-2026 which terminal this row came from. Two dishes share the
    -- route to 192.168.100.1, so without this the page's usage figure mixes kits
    -- (plan §9/F). `id` is the device_info identity; hardware_version names the
    -- model. sw_version is NOT an identity - it changes on a firmware update.
    dish_id       TEXT,
    dish_hw       TEXT,
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
-- Ada 06-Nov-2026 one row per poll per SIM, so the cycle total survives the router's counter
-- resets and can be charted (plan §6/Task 4). `total_bytes` is carried + period, which is what
-- the page shows.
-- Ada 06-Nov-2026 workstream D: the router's numbers, one row per poll. The page can only show a
-- snapshot of Delilah today; a burn rate, a failover at 03:00, or "was the signal worse before the
-- reboot" all need history. Text columns are the state that changes (active path, SSID, APN).
CREATE TABLE IF NOT EXISTS router (
    ts            REAL PRIMARY KEY,
    signal_dbm    REAL,
    noise_dbm     REAL,
    bitrate       REAL,
    link_quality  REAL,
    ssid          TEXT,
    channel       REAL,
    wwan_uptime   REAL,
    kmwan_active  TEXT,
    kmwan_level   REAL,
    kmwan_metric  REAL,
    csq           REAL,
    qcsq          TEXT,
    temp_c        REAL,
    load1         REAL,
    mem_avail     REAL,
    mem_total     REAL,
    uptime        REAL,
    rx_bytes      REAL,
    tx_bytes      REAL,
    sim_slot      INTEGER,
    traffic_sim1  REAL,
    traffic_sim2  REAL,
    cell_ok       INTEGER,
    cereg         TEXT,
    cops          TEXT,
    apn           TEXT
);
CREATE INDEX IF NOT EXISTS idx_router_ts ON router(ts);
-- Ada 06-Nov-2026 the GPS track, drained from Delilah's own log (plan §15k/§15l). `ts` is the
-- GPS's UTC time, not the router's clock, so a sample stays correct even if the router's clock
-- drifted while the link was down. It is the primary key, which is what makes a re-drain idempotent.
CREATE TABLE IF NOT EXISTS gps (
    ts     TEXT PRIMARY KEY,
    lat    REAL,
    lon    REAL,
    alt    REAL,
    speed  REAL,
    sats   INTEGER,
    hdop   REAL,
    fix    INTEGER
);
-- Ada 06-Nov-2026 places live in their own file (var/places.db), not here: van.db is the poller's
-- and van-data.service reads it with ReadOnlyPaths, so the page could never write a mark into it.
-- The track above is the poller's; the marks are the server's (plan §15l).
CREATE TABLE IF NOT EXISTS sim (
    ts           REAL PRIMARY KEY,
    slot         INTEGER,
    cycle_start  REAL,
    period_bytes INTEGER,
    carried_bytes INTEGER,
    total_bytes  INTEGER,
    cap_bytes    INTEGER,
    qgdcnt_rx    INTEGER,
    qgdcnt_tx    INTEGER
);
"""

# Ada 23-Oct-2026 the dish reports two rolling averages of its "prolonged"
# obstruction events. They are not in the table as originally written, so they
# are added if missing -- the table may already exist with rows in it, which is
# why this is an ALTER rather than a recreate.
ADD_COLUMNS = (
    # Ada 06-Nov-2026 terminal identity (see SCHEMA). Add-only, never a rename.
    ("dish_id", "TEXT"),
    ("dish_hw", "TEXT"),
    ("obstruction_duration", "REAL"),
    ("obstruction_interval", "REAL"),
    # Ada 23-Oct-2026 is_snr_above_noise_floor is a BOOLEAN, not a number, but
    # SQLite has no boolean type and the page only needs true/false/absent, so
    # INTEGER is the honest storage (1/0/NULL).
    ("is_snr_above_noise_floor", "INTEGER"),
)


def dish_terminal(status):
    """(dish id, hardware version) from a dish device_info dict.

    Both are None when the field is absent, which is the honest answer: an older sample
    without identity is not the same as "the same dish as the last one".
    """
    return (status.get("id"), status.get("hardware_version"))


# Ada 09-Nov-2026 which terminal the /van dish panel may show. Matt: "gate the dish panel on
# EvilDish's identity so the home dish can't appear there", and "ut01188c8b- is the big boy
# dish". Two Starlink kits answer 192.168.100.1, so the panel follows whichever wins the route,
# and that has been the home kit since 05-Nov, when the van's WAN jack went dark. Their counters
# have unrelated baselines, so a panel fed by both is mostly someone else's dish.
#   OTHER_DISH_IDS -- terminals identified as NOT the van's, refused whatever else is configured.
#                     BigBoyDish, the home kit. This list is what makes Matt's rule ("BigBoyDish
#                     must never appear on /van") hold, and it is the half we can populate today.
#   VAN_DISH_IDS   -- the van's own terminals (strict mode). Empty: EvilDish's device_info id has
#                     never been read -- the poller only started storing ids on 06-Nov, by which
#                     time the jack was dark -- so listing it would be a guess. While it is empty
#                     the gate runs on the denylist alone (see dish_terminal_is_van).
# The kit serial on the Starlink account page (KFV4M02179606WB8 for EvilDish, Matt 09-Nov-2026)
# CANNOT be used here: the dish's gRPC deviceInfo carries id / hardware_version /
# software_version and no serial at all. `id` is the only per-kit discriminator -- both kits
# report hardware_version "rev4_pez_proto1" -- and firmware changes under a kit, so it is not
# an identity either.
VAN_DISH_IDS = ()
OTHER_DISH_IDS = ("ut01188c8b-8281880f-1ac2a7ee",)


def dish_terminal_is_van(dish_id, van_ids=None, other_ids=None):
    """May this sample be shown on /van as the van's own terminal?

    Refused (False) when the id is a known other kit -- an explicit denylist entry outranks the
    allowlist, so a mis-pasted id cannot put the home dish back on the page. Refused again once
    VAN_DISH_IDS is populated and the id is not in it (strict membership). While VAN_DISH_IDS is
    empty -- EvilDish's id is not known yet -- anything that is not a known other kit passes, so
    the panel comes back by itself the first time EvilDish answers instead of waiting for an
    edit. None when the dish reported no identity at all, which is not the same as "the same
    dish as last time" and is refused as well.
    """
    if van_ids is None:
        van_ids = VAN_DISH_IDS
    if other_ids is None:
        other_ids = OTHER_DISH_IDS
    if not dish_id:
        return None
    if dish_id in other_ids:
        return False
    if van_ids:
        return dish_id in van_ids
    return True


# Ada 06-Nov-2026 workstream D. The probe hands back strings and several are not a single number:
# csq is "rssi,ber", load is a three-value average, link_quality is "70/70". Keep the first number
# in each case and null the rest - a chart wants one value, and the second one is not a measurement.
ROUTER_COLUMNS = (
    "signal_dbm", "noise_dbm", "bitrate", "link_quality", "ssid", "channel",
    "wwan_uptime", "kmwan_active", "kmwan_level", "kmwan_metric", "csq", "qcsq",
    "temp_c", "load1", "mem_avail", "mem_total", "uptime", "rx_bytes", "tx_bytes",
    "sim_slot", "traffic_sim1", "traffic_sim2", "cell_ok", "cereg", "cops", "apn",
)
ROUTER_TEXT = ("ssid", "kmwan_active", "qcsq", "cereg", "cops", "apn")
ROUTER_SOURCE = {"kmwan_metric": "kmwan_active_metric", "load1": "load"}


def first_num(v):
    """The first number in "18,99", "0.12 0.10 0.08" or "70/70"; None when there is none."""
    if v is None:
        return None
    for part in str(v).replace("/", " ").replace(",", " ").split():
        n = num(part)
        if n is not None:
            return n
    return None


def router_row(now, r):
    """One `router` row from a probe result: numbers where a number exists, text where it is text."""
    row = {"ts": now}
    for col in ROUTER_COLUMNS:
        v = r.get(ROUTER_SOURCE.get(col, col))
        if col in ROUTER_TEXT:
            row[col] = (str(v).strip() or None) if v is not None else None
        else:
            row[col] = first_num(v)
    return row


def sim_cycle_start(now, day=28, hour=0):
    """Epoch of the most recent SIM billing-cycle start, in the router's local time.

    The anchor is what GL itself schedules the SIM reset from (`sim2_day`/`sim2_hour`, cron
    `0 <hour> <day> * *`), so it is local, not UTC. Day 28 exists in every month, which is why
    the plan's cycle needs no month-rollover arithmetic beyond stepping back one month.
    """
    def clamp(v, lo, hi, fallback):
        try:
            v = int(v)
        except (TypeError, ValueError):
            return fallback
        return v if lo <= v <= hi else fallback

    day = clamp(day, 1, 28, 28)
    hour = clamp(hour, 0, 23, 0)
    lt = time.localtime(now)
    year, month = lt.tm_year, lt.tm_mon
    start = time.mktime((year, month, day, hour, 0, 0, 0, 0, -1))
    if start > now:
        month -= 1
        if month == 0:
            month, year = 12, year - 1
        start = time.mktime((year, month, day, hour, 0, 0, 0, 0, -1))
    return start


def sim_accumulate(prev, bytes_now, cycle_start):
    """Carry a SIM's peak forward across a counter reset. Returns the new state dict.

    GL's per-SIM counter is the sum of the module's QGDCNT deltas and it restarts whenever the
    module re-initialises (a reboot or a slot switch) - twice this cycle already, with the flash
    mirror following the reset rather than protecting against it. A drop in the counter is
    therefore treated as "everything before the drop was real": the peak is added to `carried`,
    which costs at most one sample interval per reset instead of the whole cycle.
    """
    if bytes_now is None:
        bytes_now = (prev or {}).get("last") or 0
    if not prev or prev.get("cycle_start") != cycle_start:
        return {"carried": 0, "last": bytes_now, "cycle_start": cycle_start, "total": bytes_now}
    carried = prev.get("carried") or 0
    last = prev.get("last")
    if last is not None and bytes_now < last:
        carried += last
        total = carried + bytes_now
    else:
        total = carried + bytes_now
    return {"carried": carried, "last": bytes_now, "cycle_start": cycle_start, "total": total}


def ensure_columns(con, table="dish", columns=ADD_COLUMNS):
    """Add any missing columns to an existing table, leaving its rows alone."""
    import sqlite3
    for name, typ in columns:
        try:
            con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, typ))
        except sqlite3.OperationalError:
            pass  # column already present on a re-run
    con.commit()


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


def poll_delilah(cursor=None):
    """Return a dict of Delilah readings, or raise.

    `cursor` is the newest GPS timestamp already stored here. The probe returns only rows after it,
    so an ordinary tick carries nothing and a catch-up after an outage carries the backlog in one
    reply (plan §15l). Passing it as an argument rather than re-reading the whole file is what keeps
    a 15-second poll cheap.
    """
    # pull the probe over first so the local copy is never stale
    subprocess.run(
        ["scp", "-O", "-q", "-o", "BatchMode=yes",
         os.path.join(ROOT, "bin", "delilah-probe.sh"),
         DELILAH_HOST + ":" + DELILAH_SCRIPT],
        capture_output=True, text=True, timeout=40,
    )
    cmd = "sh " + DELILAH_SCRIPT
    if cursor:
        cmd += " " + shlex.quote(str(cursor))
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         DELILAH_HOST, cmd],
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


# Ada 02-Nov-2026 alerts live 24h and are dismissible (Matt): events get a stable
# id (so a dismissal sticks) and age out on a time window instead of a 20-entry cap.
EVENT_WINDOW_S = 26 * 3600   # page shows 24h; 26h gives the browser clock-skew margin
EVENT_KEEP_MAX = 500         # display caps at 80; this bounds snapshot.json


def normalise_events(events):
    """Give every event a stable id. Events that already have one keep it, so an
    alert dismissed by id is never resurrected by a later renumbering."""
    out = []
    for e in events:
        e = dict(e)
        if not e.get("id"):
            e["id"] = str(int(e.get("ts") or 0))
        out.append(e)
    return out


# Ada 09-Nov-2026 the two dish-feed events, named so the identity gate can both withhold new ones
# and withdraw old ones. They assert something about the VAN's dish feed, and while another kit is
# the one answering they are not about the van at all (see record_events).
DISH_EVENT_MSGS = ("Dish data became stale", "Dish data recovered")


def record_events(events, prev_delilah, nd, prev_dish_stale, dish_stale, now, dish_is_van):
    """Append state-change events, then age out and cap. Returns the new list.

    Normalises on the way in, so every event this returns carries an id --
    that invariant is the whole point (a dismissal is addressed by id), and
    enforcing it here means a caller cannot forget it.

    Ada 09-Nov-2026 `dish_is_van` is this tick's verdict from dish_terminal_is_van(). Unless it is
    exactly True the dish stale/recovered pair is withheld AND any already recorded are withdrawn:
    /van shows the van's own dish or nothing, so an alert reading "Dish data became stale" above a
    panel reading "Dish Data Currently Unavailable" is the page contradicting itself. The feed such
    events describe belongs to a different kit -- on 09-Nov four of them tracked the home kit
    flapping over the WiFi repeater while Delilah was up throughout -- and its flapping is not the
    van's news. The panel's own line is the signal; it does not need an alert storm repeating it.
    """
    events = normalise_events(events)
    seq = [0]

    def _evt(msg):
        events.append({"id": "%d-%d" % (int(now), seq[0]), "ts": now, "msg": msg})
        seq[0] += 1

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
    # dish stale / recovered -- the van's own terminal only (see the docstring). Withheld when the
    # reading is refused, and any already recorded are withdrawn so the page stops contradicting
    # its own panel.
    if dish_is_van is True:
        if not prev_dish_stale and dish_stale:
            _evt(DISH_EVENT_MSGS[0])
        elif prev_dish_stale and not dish_stale:
            _evt(DISH_EVENT_MSGS[1])
    else:
        events = [e for e in events if e.get("msg") not in DISH_EVENT_MSGS]

    events = [e for e in events if now - (e.get("ts") or 0) <= EVENT_WINDOW_S]
    return events[-EVENT_KEEP_MAX:]


def main():
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(VAR, exist_ok=True)

    import sqlite3
    con = sqlite3.connect(DB, timeout=15)
    con.executescript(SCHEMA)
    # Ada 23-Oct-2026 migrate an existing table in place; Ada 06-Nov-2026 named it
    # ensure_columns() so the add-only rule is testable (tests/test_poll_dish_identity.py).
    ensure_columns(con)

    snap = load_snapshot()
    now = time.time()

    # ---- state-change notifier (Matt, 27-Oct-2026) -------------------------------
    # snapshot.json is latest-wins, so we snapshot prev-state fields from the
    # loaded snap *before* overwriting them in this tick.
    prev_delilah = snap.get("delilah") or {}
    prev_sim = snap.get("sim") or {}
    prev_dish_stale = snap.get("dish_stale", False)

    # Ada 06-Nov-2026 the GPS cursor: the newest sample we already hold. The probe returns only what
    # is newer, so a normal tick is empty and a catch-up after an outage arrives in one reply.
    try:
        _gps_cursor = con.execute("SELECT MAX(ts) FROM gps").fetchone()[0]
    except Exception:
        _gps_cursor = None

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
        # Ada 06-Nov-2026 identity of the terminal that answered this poll
        _dish_id, _dish_hw = dish_terminal(st)

        row = (
            now,
            st.get("state"),
            st.get("software_version"),
            _dish_id, _dish_hw,
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
            "ts, state, sw_version, dish_id, dish_hw, uptime_s, ping_drop, ping_latency, "
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
            # Ada 06-Nov-2026 so the page can name the terminal it is reading
            "id": _dish_id,
            "hardware_version": _dish_hw,
            # Ada 09-Nov-2026 the panel gate's own verdict, computed here and never in the page:
            # /van renders the dish only when this is exactly True (see dish_terminal_is_van).
            "van_terminal": dish_terminal_is_van(_dish_id),
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
        r = poll_delilah(_gps_cursor)
        snap["delilah"] = r
        snap["delilah_ts"] = num(r.get("collected_at") and None) or now
        snap["delilah_stale"] = False
        snap.pop("delilah_error", None)
        log("delilah ok  wwan_up=%ss active=%s ssid=%s csq=%s" % (
            r.get("wwan_uptime"), r.get("kmwan_active"), r.get("ssid"), r.get("csq")))

        # ---- SIM data meter (plan §6/Task 4) ------------------------------------
        # Ada 06-Nov-2026 read the router's per-SIM counters, roll the peak forward across a
        # reset, and keep a row per SIM per tick. The cycle anchor is the one GL resets on
        # (SIM2: day 28, per the MobileMustHave portal).
        try:
            _cycle_day = r.get("sim2_reset_day") or 28
            _cycle_hour = r.get("sim2_reset_hour") or 0
            _cycle = sim_cycle_start(now, _cycle_day, _cycle_hour)
            _prev_sims = (prev_sim or {}).get("sims") or {}
            _qgdcnt = (r.get("qgdcnt") or "").split(",")
            _rx = int(_qgdcnt[0]) if len(_qgdcnt) == 2 and _qgdcnt[0].isdigit() else None
            _tx = int(_qgdcnt[1]) if len(_qgdcnt) == 2 and _qgdcnt[1].isdigit() else None
            _sims = {}
            for _n in ("1", "2"):
                _bytes = r.get("traffic_sim" + _n)
                _bytes = int(_bytes) if (_bytes or "").isdigit() else None
                _cap = r.get("sim%s_threshold" % _n)
                _cap = int(_cap) if (_cap or "").isdigit() else None
                _st = sim_accumulate(_prev_sims.get(_n), _bytes, _cycle)
                _rec = {"label": "SIM" + _n, "slot": int(_n), "period_bytes": _bytes,
                        "carried_bytes": _st["carried"], "total_bytes": _st["total"],
                        "cap_bytes": _cap, "enabled": r.get("sim%s_enable" % _n) == "1"}
                _sims[_n] = _rec
                con.execute(
                    "INSERT OR REPLACE INTO sim (ts, slot, cycle_start, period_bytes,"
                    " carried_bytes, total_bytes, cap_bytes, qgdcnt_rx, qgdcnt_tx)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (now, int(_n), _cycle, _bytes, _st["carried"], _st["total"], _cap,
                     _rx if int(_n) == int(r.get("sim_slot") or 0) else None,
                     _tx if int(_n) == int(r.get("sim_slot") or 0) else None))
            con.commit()
            snap["sim"] = {"cycle_start": _cycle, "slot": r.get("sim_slot"),
                           "sims": _sims, "ts": now}
            log("sim ok  slot=%s sim1=%s sim2=%s (cap sim2=%s)" % (
                r.get("sim_slot"), _sims["1"]["total_bytes"], _sims["2"]["total_bytes"],
                _sims["2"]["cap_bytes"]))
        except Exception as e:
            snap["sim_error"] = str(e)[:200]
            log("sim FAILED: %s" % e)

        # ---- router history (plan §7 / workstream D) -----------------------------
        try:
            _row = router_row(now, r)
            con.execute(
                "INSERT OR REPLACE INTO router (%s) VALUES (%s)"
                % (", ".join(_row.keys()), ", ".join("?" * len(_row))),
                tuple(_row.values()))
            con.commit()
        except Exception as e:
            snap["router_error"] = str(e)[:200]
            log("router history FAILED: %s" % e)

        # ---- GPS track drain (plan §15k/§15l) ------------------------------------
        # Ada 06-Nov-2026 Delilah logs the track locally whether or not the link is up, so this is
        # the catch-up: whatever the router has that we do not, in one pass. `ts` is the primary key,
        # so a re-drain of the same rows is a no-op rather than a duplicate.
        try:
            _rows = r.get("gps") or []
            _n = 0
            for _g in _rows:
                if not isinstance(_g, list) or len(_g) < 8:
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO gps (ts, lat, lon, alt, speed, sats, hdop, fix)"
                    " VALUES (?,?,?,?,?,?,?,?)", tuple(_g[:8]))
                _n += 1
            con.commit()
            if _n:
                log("gps drained %d row(s), newest %s" % (_n, _rows[-1][0]))
        except Exception as e:
            snap["gps_error"] = str(e)[:200]
            log("gps drain FAILED: %s" % e)
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
    # Ada 02-Nov-2026 the triggers moved into record_events() so the id assignment
    # and the retention window are unit-testable (tests/test_poll_events.py).
    # Ada 09-Nov-2026 the dish pair also needs this tick's identity verdict: a reading /van is
    # refusing must not raise dish alerts (see record_events).
    snap["events"] = record_events(snap.get("events", []),
                                   prev_delilah, snap.get("delilah") or {},
                                   prev_dish_stale, snap.get("dish_stale"), now,
                                   (snap.get("dish") or {}).get("van_terminal"))
    save_snapshot(snap)
    con.close()


if __name__ == "__main__":
    main()

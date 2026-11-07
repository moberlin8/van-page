#!/usr/bin/env python3
# Ada 06-Nov-2026 GPS track logger for Delilah (GL-X3000, RM520N-GL).
#
# Why this exists: the dish refuses to hand out its position ("Disabled due to policy"), but the
# modem's GNSS does not, and a position is what turns a log into a memory - "we were here, and the
# repeater won". Plan §15k/§15l.
#
# Design notes, all of them learned the hard way:
#  * AT+QGPSLOC? is one request for lat/lon/alt/speed/UTC/satellites. No NMEA parsing, no second
#    source of truth. NMEA is still on /dev/mhi_LOOPBACK if anyone wants to watch it by hand.
#  * The timestamp comes from the FIX, not the router's clock. The router's clock drifts while the
#    link is down; the GPS's does not. That is what makes catch-up after an outage correct.
#  * No fix means no row. A null position is worse than a gap (Matt, 06-Nov-2026).
#  * Append-only CSV, not SQLite: the card and the overlay are not journaled, and a truncated last
#    line is recoverable where a torn database is not.
#  * 60 s while moving, 300 s while parked. The GPS's own speed decides, so there is no extra sensor
#    and no guessing. A year of driving is tens of megabytes.
#  * The engine is re-armed if it is found off, so a firmware update that forgets autogps heals.

import datetime
import math
import os
import re
import subprocess
import time

MODEM = ["gl_modem", "-B", "0001:01:00.0", "AT"]
DIR = "/overlay/gps"
CUR = os.path.join(DIR, "track.csv")
LOG = os.path.join(DIR, "gps.log")

MOVE_KMH = 3.0      # above this the fix is "moving"
MOVE_M = 50.0       # or if it has moved this far since the last sample
FAST = 60           # seconds between samples while moving
SLOW = 300          # seconds between samples while parked
LOG_MAX = 512 * 1024


def log(msg):
    try:
        os.makedirs(DIR, exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            with open(LOG, "r", errors="replace") as f:
                tail = f.readlines()[-200:]
            with open(LOG, "w") as f:
                f.writelines(tail)
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), msg))
    except Exception:
        pass


def at(cmd, timeout=25):
    try:
        r = subprocess.run(MODEM + [cmd], capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except Exception as e:
        log("AT %s failed: %s" % (cmd, e))
        return ""


def ensure_engine():
    """Turn the GNSS engine back on if something turned it off."""
    out = at("AT+QGPS?")
    m = re.search(r"\+QGPS:\s*(\d)", out)
    if m and m.group(1) == "0":
        at("AT+QGPS=1")
        log("engine was off; sent AT+QGPS=1")


def dm_to_dec(v):
    """'3603.4246N' -> 36.057077. Returns None if the field is not a position."""
    if not v or len(v) < 4:
        return None
    hemi = v[-1].upper()
    if hemi not in "NSEW":
        return None
    try:
        raw = float(v[:-1])
    except ValueError:
        return None
    deg = int(raw / 100)
    minutes = raw - deg * 100
    dec = deg + minutes / 60.0
    if hemi in "SW":
        dec = -dec
    return dec


def parse_loc(out):
    """+QGPSLOC: utc,lat,lon,hdop,alt,fix,cog,spkm,spkn,date,nsat -> dict or None."""
    m = re.search(r"\+QGPSLOC:\s*([^\r\n]+)", out)
    if not m:
        return None
    f = [x.strip() for x in m.group(1).split(",")]
    if len(f) < 11:
        return None
    utc, lat_s, lon_s, hdop, alt, fix, _cog, spkm, _spkn, date, nsat = f[:11]
    if not fix or fix == "0":
        return None
    lat, lon = dm_to_dec(lat_s), dm_to_dec(lon_s)
    if lat is None or lon is None:
        return None
    # date is ddmmyy, utc is hhmmss.ss
    if len(date) != 6 or len(utc) < 6:
        return None
    try:
        dd, mm, yy = int(date[0:2]), int(date[2:4]), int(date[4:6])
        hh, mi, ss = int(utc[0:2]), int(utc[2:4]), int(float(utc[4:]))
        ts = datetime.datetime(2000 + yy, mm, dd, hh, mi, ss)
    except ValueError:
        return None
    def fnum(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    return {
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lat": lat, "lon": lon,
        "alt": fnum(alt), "speed": fnum(spkm), "sats": fnum(nsat),
        "hdop": fnum(hdop), "fix": int(fix) if fix.isdigit() else None,
    }


def haversine_m(a, b, c, d):
    r = 6371000.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = p2 - p1, math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def rotate_if_stale():
    """Keep one file per month so a drain never has to read a year of rows."""
    try:
        if not os.path.exists(CUR):
            return
        mtime = datetime.datetime.utcfromtimestamp(os.path.getmtime(CUR))
        now = datetime.datetime.utcnow()
        if (mtime.year, mtime.month) != (now.year, now.month):
            dest = os.path.join(DIR, "track-%04d-%02d.csv" % (mtime.year, mtime.month))
            if not os.path.exists(dest):
                os.rename(CUR, dest)
                log("rotated to %s" % dest)
    except Exception as e:
        log("rotate failed: %s" % e)


def main():
    os.makedirs(DIR, exist_ok=True)
    log("started")
    ensure_engine()
    last = None
    last_check = 0.0
    while True:
        try:
            if time.time() - last_check > 3600:
                ensure_engine()
                rotate_if_stale()
                last_check = time.time()
            fix = parse_loc(at("AT+QGPSLOC?"))
            if fix is None:
                # No fix is normal: cold start, indoors, under cover. Say nothing, write nothing.
                time.sleep(FAST)
                continue
            moving = (fix["speed"] or 0) > MOVE_KMH
            if not moving and last:
                moving = haversine_m(last["lat"], last["lon"], fix["lat"], fix["lon"]) > MOVE_M
            with open(CUR, "a") as f:
                f.write("%s,%.6f,%.6f,%s,%s,%s,%s,%s\n" % (
                    fix["ts"], fix["lat"], fix["lon"],
                    "" if fix["alt"] is None else fix["alt"],
                    "" if fix["speed"] is None else fix["speed"],
                    "" if fix["sats"] is None else int(fix["sats"]),
                    "" if fix["hdop"] is None else fix["hdop"],
                    "" if fix["fix"] is None else fix["fix"]))
            last = fix
            time.sleep(FAST if moving else SLOW)
        except Exception as e:
            log("loop error: %s" % e)
            time.sleep(FAST)


if __name__ == "__main__":
    main()

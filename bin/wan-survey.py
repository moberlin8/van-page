#!/usr/bin/env python3
# Ada 06-Nov-2026 WAN survey for Delilah (plan §15j).
#
# Manual only (Matt): nothing here runs on a timer. It measures the paths that are already up,
# ranks them, and leaves the router on the best one - "why not let it end on the best".
#
# It runs DETACHED, because the last step can drop the link that started it: switching the active
# path tears down the ssh session that asked for the survey. Every result is written to a file, so
# the answer survives the connection that asked the question.
#
# Cost: ~1 MB per path per run, and nothing is measured that is not already up. A path that is down
# is reported as down rather than probed.
#
# What it does NOT do yet: sweep the other saved repeater SSIDs. That needs the repeater
# reconfigured and ~40 s per network, and it is the one step that can leave the van with no WiFi -
# cellular is the safety net, but it deserves its own build (plan §15j, phase 2).
import concurrent.futures
import datetime
import json
import os
import re
import subprocess
import sys
import time

OUT = "/overlay/survey"
LAST = os.path.join(OUT, "last.json")
PROBE_URL = "https://speed.cloudflare.com/__down?bytes=1000000"
PING_TARGET = "1.1.1.1"
PING_COUNT = 5
MIN_SPEED = 20000.0      # bytes/s; below this the path is not usable, whatever the ping says
MAX_LOSS = 50            # percent; above this it is not a path, it is a rumour

# id -> (label, device, kmwan member). The member is what the switch rewrites.
CANDIDATES = [
    ("wan", "WAN jack", "eth0", "wan"),
    ("wwan", "repeater", "apclix0", "wwan"),
    ("modem_0001", "cellular", "rmnet_mhi0", "modem_0001"),
]


def now_iso():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


def write_state(state, **kw):
    """Write the run file atomically: a half-written file is worse than an old one."""
    body = {"state": state, "updated": now_iso()}
    body.update(kw)
    tmp = LAST + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(body, f)
        os.replace(tmp, LAST)
    except Exception:
        pass


def active_ssid():
    out = sh("iwinfo apclix0 info 2>/dev/null")
    m = re.search(r'ESSID:\s*"([^"]*)"', out)
    return m.group(1) if m else ""


def sim_slot():
    out = sh("gl_modem -B 0001:01:00.0 AT 'AT+QUIMSLOT?' 2>/dev/null")
    m = re.search(r"\+QUIMSLOT:\s*(\d+)", out)
    return m.group(1) if m else ""


def candidates():
    """The paths that exist on this router, labelled for a human."""
    ssid = active_ssid()
    slot = sim_slot()
    out = []
    for cid, label, dev, member in CANDIDATES:
        if cid == "wwan" and ssid:
            label = "repeater (%s)" % ssid
        if cid == "modem_0001" and slot:
            label = "cellular (SIM %s)" % slot
        out.append({"id": cid, "label": label, "device": dev, "member": member})
    return out


def link_state(dev):
    """(up, ipv4) for a device, from one `ip` call."""
    out = sh("ip -br addr show %s 2>/dev/null" % dev, timeout=10)
    if not out.strip():
        return False, None
    parts = out.split()
    up = len(parts) > 1 and parts[1] == "UP"
    ip4 = None
    for p in parts[2:]:
        if "." in p and "/" in p:
            ip4 = p.split("/")[0]
            break
    return up, ip4


def measure(c):
    """One path: link, latency, loss, and a 1 MB download through that interface."""
    res = dict(c)
    res.update({"ok": False, "reason": None, "loss_pct": None, "rtt_ms": None,
                "rtt_min": None, "rtt_max": None, "speed_bps": None, "http_code": None,
                "bytes": 1000000})
    up, ip4 = link_state(c["device"])
    res["ip"] = ip4
    if not up:
        res["reason"] = "no link"
        return res
    if not ip4:
        res["reason"] = "no address"
        return res

    p = sh("ping -I %s -c %d -W 2 -q %s 2>/dev/null" % (c["device"], PING_COUNT, PING_TARGET),
           timeout=PING_COUNT * 3 + 5)
    m = re.search(r"(\d+)% packet loss", p)
    if m:
        res["loss_pct"] = int(m.group(1))
    m = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)", p)
    if m:
        res["rtt_min"], res["rtt_ms"], res["rtt_max"] = (float(m.group(1)), float(m.group(2)),
                                                         float(m.group(3)))

    # --interface is what makes this a measurement of THIS path rather than of the default route.
    out = sh("curl --interface %s -s -o /dev/null --max-time 25 "
             "-w '%%{speed_download} %%{time_total} %%{http_code}' '%s' 2>/dev/null"
             % (c["device"], PROBE_URL), timeout=35)
    parts = out.split()
    if len(parts) >= 3:
        try:
            res["speed_bps"] = float(parts[0])
            res["time_s"] = float(parts[1])
            res["http_code"] = int(parts[2])
        except ValueError:
            pass

    if res["http_code"] != 200:
        res["reason"] = "probe failed (HTTP %s)" % res["http_code"]
    elif (res["speed_bps"] or 0) < MIN_SPEED:
        res["reason"] = "too slow to use (%.0f KB/s)" % ((res["speed_bps"] or 0) / 1000.0)
    elif (res["loss_pct"] or 0) > MAX_LOSS:
        res["reason"] = "%d%% packet loss" % res["loss_pct"]
    else:
        res["ok"] = True
    return res


def rank(results):
    """Best first. Throughput decides, latency breaks the tie, failures go last.

    Deliberately simple and explainable: "the dish won on throughput" is a sentence Matt can check
    against the numbers, where a weighted score is a number nobody can argue with.
    """
    def key(r):
        return (0 if r["ok"] else 1,
                -(r["speed_bps"] or 0),
                r["rtt_ms"] if r["rtt_ms"] is not None else 9999)
    return sorted(results, key=key)


def current_metrics():
    out = {}
    for _cid, _label, _dev, member in CANDIDATES:
        v = sh("uci -q get kmwan.%s.metric" % member, timeout=10).strip()
        out[member] = int(v) if v.isdigit() else None
    return out


def switch(winner_member, results):
    """Put the winner first in kmwan's failover order, keeping the others' relative order.

    kmwan in failover mode picks the lowest metric, so this is the whole switch. The previous
    metrics are returned so the change is reversible and visible in the result.
    """
    before = current_metrics()
    # A SWAP, not a renumber. Renumbering 1,2,3 would push the WAN jack below cellular as a side
    # effect of a survey that had nothing to do with it - and the jack is the Starlink path, which
    # should outrank cellular whenever it is plugged in. Only the two members involved move.
    plan = dict(before)
    if before.get(winner_member) == 1:
        plan = before
    else:
        old = before.get(winner_member)
        for m, v in before.items():
            if v == 1:
                plan[m] = old if old is not None else 2
        plan[winner_member] = 1
    changed = {m: v for m, v in plan.items() if before.get(m) != v}
    if not changed:
        return {"switched": False, "reason": "already first", "before": before, "after": plan}
    for m, v in plan.items():
        sh("uci set kmwan.%s.metric=%d" % (m, v), timeout=10)
    sh("uci commit kmwan", timeout=10)
    sh("/etc/init.d/kmwan restart", timeout=60)
    return {"switched": True, "before": before, "after": plan}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--no-switch" in sys.argv
    name = args[0] if args else ""
    run_id = args[1] if len(args) > 1 else str(int(time.time()))
    os.makedirs(OUT, exist_ok=True)

    cands = candidates()
    write_state("measuring", run_id=run_id, name=name, started=now_iso(),
                candidates=[{"id": c["id"], "label": c["label"]} for c in cands])

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cands)) as ex:
        for r in ex.map(measure, cands):
            results.append(r)

    ranked = rank(results)
    winner = ranked[0] if ranked and ranked[0]["ok"] else None

    # Write the measurements BEFORE switching: the switch can take the link down, and the numbers
    # are the part that cannot be re-derived.
    write_state("switching", run_id=run_id, name=name, started=now_iso(),
                results=ranked, winner=winner["id"] if winner else None,
                winner_label=winner["label"] if winner else None)

    sw = {"switched": False, "reason": "no usable path"}
    if winner and dry:
        sw = {"switched": False, "reason": "dry run: measurement only"}
    elif winner:
        sw = switch(winner["member"], ranked)

    write_state("done", run_id=run_id, name=name, started=now_iso(), finished=now_iso(),
                results=ranked, winner=winner["id"] if winner else None,
                winner_label=winner["label"] if winner else None,
                switch=sw, cost_bytes=sum(r.get("bytes") or 0 for r in results if r["ok"]))


if __name__ == "__main__":
    main()

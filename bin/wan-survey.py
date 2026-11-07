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
    out = sh("iwinfo %s info 2>/dev/null" % wwan_device())
    m = re.search(r'ESSID:\s*"([^"]*)"', out)
    return m.group(1) if m else ""


def sim_slot():
    out = sh("gl_modem -B 0001:01:00.0 AT 'AT+QUIMSLOT?' 2>/dev/null")
    m = re.search(r"\+QUIMSLOT:\s*(\d+)", out)
    return m.group(1) if m else ""


def wwan_device():
    """The repeater interface that currently has an address.

    There are TWO: apclix0 is the 5 GHz client, apcli0 the 2.4 GHz one, and only the band that is
    associated has an address. Checking only apclix0 made a perfectly good 2.4 GHz association look
    like a failed connect - which is exactly what happened to Buff.
    """
    for dev in ("apclix0", "apcli0"):
        up, ip4 = link_state(dev)
        if up and ip4:
            return dev
    return "apclix0"


def candidates():
    """The paths that exist on this router, labelled for a human."""
    ssid = active_ssid()
    slot = sim_slot()
    out = []
    for cid, label, dev, member in CANDIDATES:
        if cid == "wwan":
            dev = wwan_device()
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



# ---- the SSID sweep (Matt, 06-Nov-2026) -------------------------------------
# Start from the networks the router is already configured for, not from everything in the air:
# those are the only ones it can join, and the filter is free. The scan itself cannot be targeted
# from outside - the ubus API has no "scan for this SSID" - so it is one broad scan, intersected
# with the configured list.
#
# The scan needs a contiguous 128 KB block (order 5) in atomic context to hand its results back,
# and on this box that allocation fails under memory pressure: the kernel logs
# "page allocation failure: order:5" at RTMPIoctlGetSiteSurvey and the survey comes back empty.
# Dropping the page cache is what makes the block available again, so it is done before every scan
# and the scan is retried once if it still comes back empty.

def configured_ssids():
    """[{ssid, key}] from /etc/config/repeater - the networks this router can actually join."""
    out = sh("uci show repeater 2>/dev/null", timeout=10)
    nets, cur = [], {}
    for line in out.splitlines():
        if "=network" in line:
            if cur.get("ssid"):
                nets.append(cur)
            cur = {}
        elif ".ssid=" in line:
            cur["ssid"] = line.split("=", 1)[1].strip().strip("'")
        elif ".key=" in line:
            cur["key"] = line.split("=", 1)[1].strip().strip("'")
    if cur.get("ssid"):
        nets.append(cur)
    return nets


def _one_scan():
    sh("sync; echo 3 > /proc/sys/vm/drop_caches", timeout=20)
    raw = sh("ubus call repeater scan '{\"cached\":false}' 2>/dev/null", timeout=40)
    try:
        return json.loads(raw).get("survey") or []
    except ValueError:
        return []


def scan_ssids(wanted=()):
    """{ssid: {signal, channel, band, bssid}} for everything in the air, best BSS per SSID.

    Retried while any configured SSID is still missing, not just while the result is empty: the
    failure mode here is a PARTIAL scan (the driver's result buffer is allocated per scan and can
    come back short), and a partial scan that happens to contain one configured network looks
    exactly like a complete one that contains one.
    """
    rows = _one_scan()
    for _ in range(2):
        found = {(r.get("ssid") or "").strip() for r in rows}
        if all(w in found for w in wanted):
            break
        time.sleep(2)
        more = _one_scan()
        if len(more) > len(rows):
            rows = more
    best = {}
    for r in rows:
        ssid = (r.get("ssid") or "").strip()
        if not ssid:
            continue
        sig = r.get("signal")
        if ssid not in best or (sig is not None and sig > (best[ssid].get("signal") or -999)):
            best[ssid] = {"signal": sig, "channel": r.get("channel"),
                          "band": r.get("band"), "bssid": r.get("bssid")}
    return best


def current_ssid():
    out = sh("ubus call repeater status 2>/dev/null", timeout=15)
    m = re.search(r'"ssid":\s*"([^"]*)"', out)
    return m.group(1) if m else ""


def current_link():
    """The association actually in use: ssid, signal, channel, band, bssid.

    The scan reports the best BSS per SSID, which for a dual-band network is often the OTHER band
    from the one associated - so the current network's row is taken from the association, not from
    the scan, or the report would claim a channel the measurement never used.
    """
    out = sh("ubus call repeater status 2>/dev/null", timeout=15)
    try:
        d = json.loads(out)
    except ValueError:
        return {}
    ch = d.get("channel")
    return {"ssid": d.get("ssid"), "signal": d.get("signal"), "channel": ch,
            "band": "5g" if isinstance(ch, int) and ch > 14 else "2g",
            "bssid": d.get("bssid")}


def connect_ssid(ssid, key):
    """Ask the repeater to join `ssid`. Returns True if it came up with an address."""
    payload = json.dumps({"ssid": ssid, "key": key or "", "remember": True})
    sh("ubus call repeater connect '%s' 2>/dev/null" % payload.replace("'", "'\\''"), timeout=30)
    deadline = time.time() + 35
    while time.time() < deadline:
        time.sleep(3)
        for dev in ("apclix0", "apcli0"):
            up, ip4 = link_state(dev)
            if up and ip4:
                return True
    return False


def wait_usable(timeout=45):
    """Wait until the repeater has an address AND answers a ping.

    An address alone is not a path: right after a reconnect the interface can hold a lease while
    the radio is still settling, and measuring then reports "no link" for a network that is fine.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        dev = wwan_device()
        up, ip4 = link_state(dev)
        if up and ip4:
            out = sh("ping -I %s -c 1 -W 2 -q 1.1.1.1 2>/dev/null" % dev, timeout=8)
            if "1 received" in out or "0% packet loss" in out:
                return True
        time.sleep(3)
    return False


def sweep(visible, current):
    """Measure each visible configured SSID that is not the one already in use.

    This is the disruptive part: each attempt drops the current WiFi for up to ~35 s. Cellular is
    the safety net, and the caller reconnects to the best at the end, so the van is never left
    without a path.
    """
    out = []
    for net in visible:
        ssid = net["ssid"]
        rec = {"ssid": ssid, "signal": net.get("signal"), "channel": net.get("channel"),
               "band": net.get("band"), "connected": False, "ok": False, "reason": None,
               "speed_bps": None, "rtt_ms": None, "loss_pct": None}
        if ssid == current:
            # Already associated: measure it where it stands. Every candidate is then measured on
            # the same terms, which is the only way "best" means anything.
            rec["connected"] = True
            st = current_link()
            if st:
                rec.update({"signal": st.get("signal"), "channel": st.get("channel"),
                            "band": st.get("band"), "bssid": st.get("bssid")})
        elif not connect_ssid(ssid, net.get("key")):
            rec["reason"] = "did not come up (no address within 35s)"
            out.append(rec)
            continue
        else:
            rec["connected"] = True
        # An address is not a path. After a reconnect the radio is still settling, and measuring
        # then reports "no link" for a network that is fine - which is what made Buff look broken
        # in the run before this one.
        if not wait_usable(30):
            rec["reason"] = "associated but not passing traffic"
            out.append(rec)
            continue
        m = measure({"id": "wwan", "label": ssid, "device": wwan_device(), "member": "wwan"})
        rec.update({"ok": m["ok"], "reason": m["reason"], "speed_bps": m["speed_bps"],
                    "rtt_ms": m["rtt_ms"], "loss_pct": m["loss_pct"]})
        out.append(rec)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--no-switch" in sys.argv
    name = args[0] if args else ""
    run_id = args[1] if len(args) > 1 else str(int(time.time()))
    os.makedirs(OUT, exist_ok=True)

    cands = candidates()
    write_state("scanning", run_id=run_id, name=name, started=now_iso(),
                candidates=[{"id": c["id"], "label": c["label"]} for c in cands])

    # ---- the SSID sweep (Matt, 06-Nov-2026) ---------------------------------
    # Only the networks this router is configured for are worth checking, and only the ones the
    # scan can actually see are worth trying. The scan is one broad call intersected with the
    # configured list, because the ubus API has no targeted scan.
    configured = configured_ssids()
    air = scan_ssids([n["ssid"] for n in configured])
    cur = current_ssid()
    visible = []
    for net in configured:
        info = air.get(net["ssid"])
        if info:
            visible.append(dict(net, **info))
    write_state("sweeping", run_id=run_id, name=name, started=now_iso(),
                configured=[n["ssid"] for n in configured],
                visible=[{"ssid": v["ssid"], "signal": v.get("signal"),
                          "channel": v.get("channel"), "band": v.get("band")} for v in visible],
                current_ssid=cur, scan_count=len(air))

    swept = []
    if not dry:
        swept = sweep(visible, cur)
        # Leave the repeater on the best SSID it can actually reach. The sweep walks away from the
        # current network, so this is what puts it back - or moves it somewhere better.
        best = None
        for r in swept:
            if r["ok"] and (best is None or (r["speed_bps"] or 0) > (best["speed_bps"] or 0)):
                best = r
        target = best["ssid"] if best else cur
        if target and target != current_ssid():
            key = next((n.get("key") for n in configured if n["ssid"] == target), "")
            connect_ssid(target, key)
        # The sweep walks away from the current network, so the path measurement below must not
        # start until the one it settled on is actually carrying traffic.
        wait_usable()
        # Rebuild the candidate list: the repeater's label and device are whatever it ended on.
        cands = candidates()

    write_state("measuring", run_id=run_id, name=name, started=now_iso(),
                candidates=[{"id": c["id"], "label": c["label"]} for c in cands],
                swept=swept, configured=[n["ssid"] for n in configured],
                visible=[v["ssid"] for v in visible], current_ssid=cur)

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
                switch=sw, swept=swept, configured=[n["ssid"] for n in configured],
                visible=[v["ssid"] for v in visible], current_ssid=cur,
                cost_bytes=(sum(r.get("bytes") or 0 for r in results if r["ok"])
                            + sum(1000000 for r in swept if r["ok"])))


if __name__ == "__main__":
    main()

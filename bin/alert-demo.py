#!/usr/bin/env python3
"""## Ada 02-Nov-2026 emit synthetic alerts into the live snapshot so the /van
alert UI can be exercised end to end (Matt).

This is a UI test harness, not a feature. It writes events into the SAME channel
the poller uses -- `snapshot.json`'s `events` array -- so everything downstream is
the real thing: the real 24h window, the real render, the real scroll box and cap,
and the real shared dismissal store. Nothing here is a mock.

Why this works: `poll.record_events()` PRESERVES the events it finds and only ages
out those older than the 26h window, so injected alerts survive every poll tick.
Writing them anywhere else (a side file) would need a page change; writing them
here needs none.

Every message is prefixed "DEMO: " and every id with "demo-" so nobody -- Matt,
Hal, or a future session -- mistakes these for real state changes. `--clear`
removes exactly those and nothing else.

Usage:
  alert-demo.py                 # 6 alerts, ages spread over the last few minutes
  alert-demo.py --count 25      # enough to scroll the box (20 rows fit)
  alert-demo.py --flood         # 85 alerts: shows the 80-row cap and the count line
  alert-demo.py --interval 20   # keep emitting one every 20s until Ctrl-C
  alert-demo.py --clear         # remove every synthetic alert
"""
import argparse
import json
import os
import random
import sys
import time

SNAPSHOT = "/srv/van/data/snapshot.json"
ID_PREFIX = "demo-"
MSG_PREFIX = "DEMO: "

SSIDS = ["Valhalla", "Buff", "Buff5G", "Placement2000"]
MEMBERS = ["wan", "wwan", "modem_0001"]


def template(rng):
    """One message in the shape a real poll tick produces."""
    choice = rng.randrange(6)
    if choice == 0:
        a, b = rng.sample(SSIDS, 2)
        return "WiFi SSID changed from %s to %s" % (a, b)
    if choice == 1:
        a, b = rng.sample(MEMBERS, 2)
        return "Failover %s to %s" % (a, b)
    if choice == 2:
        return "WAN Port %s" % rng.choice(["up", "down"])
    if choice == 3:
        return "Cellular %s" % rng.choice(["active", "inactive"])
    if choice == 4:
        return "SIM changed (IMSI %s -> %s)" % (
            rng.choice(["454351016341207", "206018065119718"]),
            rng.choice(["454351016341207", "206018065119718"]),
        )
    return "Dish data %s" % rng.choice(["became stale", "recovered"])


def load():
    """The live snapshot, or an empty dict if it is not there yet."""
    try:
        with open(SNAPSHOT) as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save(snap):
    """Atomic, exactly like poll.save_snapshot: temp file then os.replace, so a
    reader (the page, every 15s) can never see a half-written file."""
    tmp = SNAPSHOT + ".demo.tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, indent=1, sort_keys=True)
    os.replace(tmp, SNAPSHOT)


def add(count, spread_s, rng):
    now = time.time()
    snap = load()
    events = list(snap.get("events") or [])
    before = len([e for e in events if str(e.get("id", "")).startswith(ID_PREFIX)])
    for i in range(count):
        # newest first, each a bit older than the last, so the ages read naturally
        ts = now - (i * spread_s)
        events.append({
            "id": "%s%d-%d" % (ID_PREFIX, int(now), i),
            "ts": ts,
            "msg": MSG_PREFIX + template(rng),
        })
    snap["events"] = events
    save(snap)

    # read back: a write that silently did not land is the one failure this
    # harness must not hide, because the whole point is to look at the page.
    check = load()
    written = [e for e in (check.get("events") or [])
               if str(e.get("id", "")).startswith(ID_PREFIX)]
    return before, len(written), len(check.get("events") or [])


def clear():
    snap = load()
    events = list(snap.get("events") or [])
    kept = [e for e in events if not str(e.get("id", "")).startswith(ID_PREFIX)]
    removed = len(events) - len(kept)
    snap["events"] = kept
    save(snap)
    check = load()
    return removed, len(check.get("events") or [])


def main():
    ap = argparse.ArgumentParser(description="emit synthetic /van alerts (UI test)")
    ap.add_argument("--count", type=int, default=6,
                    help="how many alerts to add (default 6; 21+ scrolls the box)")
    ap.add_argument("--spread", type=float, default=95.0,
                    help="seconds between the alerts' timestamps (default 95)")
    ap.add_argument("--flood", action="store_true",
                    help="85 alerts spread over 20h: shows the 80-row cap")
    ap.add_argument("--interval", type=float, default=0,
                    help="keep emitting one every N seconds until interrupted")
    ap.add_argument("--clear", action="store_true",
                    help="remove every synthetic alert and exit")
    ap.add_argument("--seed", type=int, default=None, help="deterministic messages")
    args = ap.parse_args()

    if not os.path.exists(os.path.dirname(SNAPSHOT)):
        sys.exit("no %s -- is the poller running?" % os.path.dirname(SNAPSHOT))

    rng = random.Random(args.seed)

    if args.clear:
        removed, left = clear()
        print("removed %d synthetic alert(s); %d event(s) left" % (removed, left))
        return

    if args.interval:
        print("emitting one synthetic alert every %gs until interrupted "
              "(Ctrl-C), then run --clear" % args.interval)
        try:
            while True:
                before, written, total = add(1, 0, rng)
                print("  +1  (synthetic now %d, events in snapshot %d)" % (written, total),
                      flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped")
            return

    if args.flood:
        # 85 across 20h: inside the page's 24h window, past its 80-row cap
        before, written, total = add(85, 20 * 3600 / 85.0, rng)
    else:
        before, written, total = add(args.count, args.spread, rng)

    print("added %d; snapshot now holds %d event(s), %d of them synthetic"
          % (written - before, total, written))
    print("page: https://harlan.cinnamon-pinecone.ts.net:8888/van/index.html "
          "(hard-refresh; the alert row updates on its 15s tick)")
    print("remove them with: %s --clear" % os.path.basename(__file__))


if __name__ == "__main__":
    main()

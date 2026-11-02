# Van Alert Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the /van state-change alerts live 24 h, dismissible individually or all at once, with dismissal state shared across devices.

**Architecture:** Events gain a stable id and a 26 h retained window in the poller. A separate `dismissed.json`, owned by `serve-data.py` (never opened by the poller), holds the shared dismissal map behind three routes. The page filters events by window + dismissal, renders up to 80 rows in a scroll box, and hides rows optimistically until the next `/dismissed` fetch confirms.

**Tech Stack:** Python 3 stdlib (`unittest`, `http.server`, `json`, `os`), inline browser JS, node (`node:test`) for the JS helper tests. No new dependencies.

**Spec:** `/home/ada/van/docs/2026-11-02-van-alert-lifecycle-design.md`

## Global Constraints

- Page retention window: **86400 s (24 h)**. Store retention window: **93600 s (26 h)**.
- Store keep cap: **500** events. Page display cap: **80** rows. No-scrollbar threshold: **20** rows.
- Dismissal file: `/srv/van/data/dismissed.json`, shape `{"ids": {"<id>": <dismissed_ts>}}`.
- `/dismissed` MUST be served `Cache-Control: no-store`. snapshot.json keeps `max-age=60`.
- Event id: `"%d-%d" % (int(ts), seq)`; seq counts events appended within one tick. Legacy entries lacking an id normalise to `str(int(ts))` and keep it.
- **The poller must never open `dismissed.json`.**
- `serve-data.py` CORS must allow **POST** (currently `GET, OPTIONS`) and keep `Access-Control-Allow-Origin: *`.
- Every changed file carries a top-of-file `## Ada 02-Nov-2026 <what changed>` comment.
- Two repos: poller `bin/*` + `tests/` in `/home/ada/van`; page `index.html` + its tests in `/srv/van`. Commit in the right one.

## Review Focus

1. **A dismissal landing while the poller is mid-tick** — expected: the dismissal survives and the alert does not return. Owned by Task 3 (poller never opens the file) and proven in Task 6.
2. **Cached `/dismissed`** — expected: `no-store`, otherwise the alert flashes back within one 15 s tick. Owned by Task 3, test asserts the header.
3. **"Close all" racing a new alert** — expected: an alert that fired after the page rendered is NOT dismissed. Owned by Task 3.
4. **Missing or corrupt `dismissed.json`** — expected: treated as empty, page still renders, no 500. Owned by Task 2.
5. **More than 80 events in 24 h** — expected: display caps at the newest 80, the true total is reported, and the store keeps the newest 500. Owned by Tasks 1 and 4.

---

### Task 1: Event identity and retention window in the poller

**Files:**
- Modify: `/home/ada/van/bin/poll.py` (add constants + two functions above `main()`; replace the events block currently at lines 408-442)
- Test: `/home/ada/van/tests/test_poll_events.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `EVENT_WINDOW_S = 93600`, `EVENT_KEEP_MAX = 500`, `normalise_events(events) -> list[dict]`, `record_events(events, prev_delilah, nd, prev_dish_stale, dish_stale, now) -> list[dict]`. Events are dicts `{"id": str, "ts": float, "msg": str}`.

- [ ] **Step 1: Write the failing test**

```python
# /home/ada/van/tests/test_poll_events.py
import sys
import unittest

sys.path.insert(0, "/home/ada/van/bin")
import poll


class TestRecordEvents(unittest.TestCase):
    def test_no_change_appends_nothing(self):
        self.assertEqual(poll.record_events([], {}, {}, False, False, 1000.0), [])

    def test_ssid_change_appends_one_ided_event(self):
        ev = poll.record_events([], {"ssid": "A"}, {"ssid": "B"}, False, False, 1000.0)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["id"], "1000-0")
        self.assertEqual(ev[0]["msg"], "WiFi SSID changed from A to B")

    def test_two_triggers_in_one_tick_get_distinct_ids(self):
        ev = poll.record_events([], {"ssid": "A", "kmwan_active": "wan"},
                                {"ssid": "B", "kmwan_active": "wwan"}, False, False, 1000.0)
        self.assertEqual([e["id"] for e in ev], ["1000-0", "1000-1"])

    def test_legacy_event_without_id_gets_ts_id_and_keeps_it(self):
        ev = poll.record_events([{"ts": 5, "msg": "old"}], {}, {}, False, False, 1000.0)
        self.assertEqual(ev[0]["id"], "5")
        ev2 = poll.record_events(ev, {}, {}, False, False, 1001.0)
        self.assertEqual(ev2[0]["id"], "5")

    def test_event_older_than_window_is_dropped(self):
        old = {"ts": 1000.0 - poll.EVENT_WINDOW_S - 1, "msg": "ancient"}
        keep = {"ts": 1000.0 - 3600, "msg": "recent"}
        ev = poll.record_events([old, keep], {}, {}, False, False, 1000.0)
        self.assertEqual([e["msg"] for e in ev], ["recent"])

    def test_store_cap_keeps_the_newest(self):
        evs = [{"ts": 1000.0 - i, "msg": "m%d" % i} for i in range(poll.EVENT_KEEP_MAX + 10)]
        out = poll.record_events(evs, {}, {}, False, False, 1000.0)
        self.assertEqual(len(out), poll.EVENT_KEEP_MAX)
        self.assertEqual(out[0]["msg"], "m10")
        self.assertEqual(out[-1]["msg"], "m0")

    def test_dish_recovery_is_recorded(self):
        ev = poll.record_events([], {}, {}, True, False, 1000.0)
        self.assertEqual([e["msg"] for e in ev], ["Dish data recovered"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ada/van && python3 -m unittest discover -s tests -v`
Expected: FAIL — `AttributeError: module 'poll' has no attribute 'EVENT_WINDOW_S'`.

- [ ] **Step 3: Implement in `/home/ada/van/bin/poll.py`**

Add the `## Ada 02-Nov-2026 ...` header line. Add above `main()`:

```python
EVENT_WINDOW_S = 26 * 3600   # page shows 24h; 26h gives the browser clock-skew margin
EVENT_KEEP_MAX = 500         # display caps at 80; this bounds snapshot.json


def normalise_events(events):
    """Give every event a stable id. Events that already have one keep it."""
    out = []
    for e in events:
        e = dict(e)
        if not e.get("id"):
            e["id"] = str(int(e.get("ts") or 0))
        out.append(e)
    return out


def record_events(events, prev_delilah, nd, prev_dish_stale, dish_stale, now):
    """Append state-change events, then age out and cap. Returns the new list."""
```
The body keeps the existing six triggers verbatim from lines 408-442, but `_evt` becomes:
```python
    def _evt(msg):
        events.append({"id": "%d-%d" % (int(now), seq[0]), "ts": now, "msg": msg})
        seq[0] += 1
```
with `seq = [0]` initialised before the triggers, and the function ending:
```python
    events = [e for e in events if now - (e.get("ts") or 0) <= EVENT_WINDOW_S]
    return events[-EVENT_KEEP_MAX:]
```
Call it from `main()` in place of the block, keeping `normalise_events` first:
```python
    snap["events"] = record_events(normalise_events(snap.get("events", [])),
                                   prev_delilah, snap.get("delilah") or {},
                                   prev_dish_stale, snap.get("dish_stale"), now)
```
`prev_delilah` and `prev_dish_stale` already exist earlier in `main()` (lines 270-271).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ada/van && python3 -m unittest discover -s tests -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/ada/van && git add bin/poll.py tests/test_poll_events.py
git commit -m "van poll: stable event ids + 26h retention window (was a 20-entry cap)"
```

---

### Task 2: The dismissal store

**Files:**
- Modify: `/home/ada/van/bin/serve-data.py` (constants + five functions near the other readers, above `class Handler`)
- Test: `/home/ada/van/tests/test_serve_dismiss.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `DISMISSED` (path), `DISMISS_WINDOW_S = 93600`, `read_dismissed(path=None) -> dict[str, float]`, `prune_dismissed(ids, now=None) -> dict[str, float]`, `write_dismissed(ids, path=None) -> None`, `dismiss_ids(id_list, path=None, now=None) -> dict[str, float]`.
- **`path=None` is deliberate, not style.** A default of `path=DISMISSED` binds the value at import time, so a test that patches `serve_data.DISMISSED` would still write to the real `/srv/van/data/dismissed.json`. Each function must resolve `path` inside the body: `if path is None: path = DISMISSED`.

- [ ] **Step 1: Write the failing test**

```python
# /home/ada/van/tests/test_serve_dismiss.py
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "/home/ada/van/bin")
import serve_data as sd


class TestDismissStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "dismissed.json")

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(sd.read_dismissed(self.path), {})

    def test_corrupt_file_reads_as_empty(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(sd.read_dismissed(self.path), {})

    def test_write_then_read_round_trips(self):
        sd.write_dismissed({"a-1": 100.0}, self.path)
        self.assertEqual(sd.read_dismissed(self.path), {"a-1": 100.0})

    def test_write_leaves_no_temp_file(self):
        sd.write_dismissed({"a-1": 100.0}, self.path)
        self.assertEqual(sorted(os.listdir(self.dir)), ["dismissed.json"])

    def test_prune_drops_entries_outside_the_window(self):
        ids = {"old": 1000.0 - sd.DISMISS_WINDOW_S - 1, "new": 1000.0 - 10}
        self.assertEqual(sd.prune_dismissed(ids, now=1000.0), {"new": 990.0})

    def test_dismiss_ids_adds_and_prunes(self):
        sd.write_dismissed({"stale": 1000.0 - sd.DISMISS_WINDOW_S - 1}, self.path)
        out = sd.dismiss_ids(["a-1", "a-2"], path=self.path, now=1000.0)
        self.assertEqual(sorted(out), ["a-1", "a-2"])
        self.assertEqual(sorted(sd.read_dismissed(self.path)), ["a-1", "a-2"])

    def test_dismiss_ids_ignores_non_strings(self):
        out = sd.dismiss_ids(["ok", None, 7, ""], path=self.path, now=1000.0)
        self.assertEqual(sorted(out), ["ok"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ada/van && python3 -m unittest tests.test_serve_dismiss -v`
Expected: FAIL — `AttributeError: module 'serve_data' has no attribute 'read_dismissed'`.

- [ ] **Step 3: Implement in `/home/ada/van/bin/serve-data.py`**

Add the `## Ada 02-Nov-2026 ...` header line, then near `DB`:
```python
DISMISSED = os.path.join(ROOT, "dismissed.json")
DISMISS_WINDOW_S = 26 * 3600
```
and above `class Handler`:
```python
def read_dismissed(path=DISMISSED):
    """The shared dismissal map {id: dismissed_ts}. A missing or unreadable file
    is an empty map, never an error -- the page must render regardless."""


def prune_dismissed(ids, now=None):
    """Drop ids dismissed longer ago than the retention window."""


def write_dismissed(ids, path=DISMISSED):
    """Atomic write: temp file in the same dir, then os.replace."""


def dismiss_ids(id_list, path=DISMISSED, now=None):
    """Add ids to the map, prune, write, and return the new map."""
```
`read_dismissed` returns `data.get("ids") or {}` when the JSON is a dict with an `ids` key, `{}` on any exception, and coerces values to `float`. `write_dismissed` writes `{"ids": ids}` via a `path + ".tmp"` then `os.replace`, and creates the parent dir if needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ada/van && python3 -m unittest tests.test_serve_dismiss -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/ada/van && git add bin/serve-data.py tests/test_serve_dismiss.py
git commit -m "van data: shared dismissal store (atomic write, 26h prune)"
```

---

### Task 3: Dismissal routes, CORS and no-store

**Files:**
- Modify: `/home/ada/van/bin/serve-data.py` (`end_headers`, `do_POST`, `do_GET`)
- Test: `/home/ada/van/tests/test_serve_routes.py`

**Interfaces:**
- Consumes: `read_dismissed`, `dismiss_ids` from Task 2.
- Produces: routes `GET /dismissed` → `{"ids": {...}}` + `Cache-Control: no-store`; `POST /dismiss` `{"id": str}` → `{"ok": true, "ids": {...}}`; `POST /dismiss_all` `{"ids": [str]}` → `{"ok": true, "count": int, "ids": {...}}`; both POSTs answer **400** `{"error": ...}` on malformed JSON or a missing `id`.

- [ ] **Step 1: Write the failing test**

```python
# /home/ada/van/tests/test_serve_routes.py
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, "/home/ada/van/bin")
import serve_data as sd


class TestDismissRoutes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # save/restore: unittest discover runs every module in one process, so a
        # leaked module global would follow other suites around.
        self._saved = (sd.ROOT, sd.DISMISSED)
        sd.ROOT = self.dir
        sd.DISMISSED = os.path.join(self.dir, "dismissed.json")
        self.server = sd.Server(("127.0.0.1", 0), sd.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        sd.ROOT, sd.DISMISSED = self._saved

    def _req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body) if body is not None else None
        c.request(method, path, payload, {"Content-Type": "application/json"})
        r = c.getresponse()
        data, headers = r.read(), dict(r.getheaders())
        c.close()
        return r.status, headers, data

    def test_get_dismissed_is_empty_and_no_store(self):
        status, headers, body = self._req("GET", "/dismissed")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(json.loads(body)["ids"], {})

    def test_post_dismiss_persists(self):
        status, _, body = self._req("POST", "/dismiss", {"id": "1000-0"})
        self.assertEqual(status, 200)
        self.assertIn("1000-0", json.loads(body)["ids"])
        self.assertEqual(sorted(sd.read_dismissed(sd.DISMISSED)), ["1000-0"])

    def test_post_dismiss_without_id_is_400(self):
        status, _, _ = self._req("POST", "/dismiss", {})
        self.assertEqual(status, 400)

    def test_post_dismiss_all_only_dismisses_given_ids(self):
        self._req("POST", "/dismiss", {"id": "keep-me-not"})
        status, _, body = self._req("POST", "/dismiss_all", {"ids": ["a-1", "a-2"]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["count"], 2)
        got = sorted(sd.read_dismissed(sd.DISMISSED))
        self.assertEqual(got, ["a-1", "a-2", "keep-me-not"])

    def test_cors_allows_post(self):
        _, headers, _ = self._req("GET", "/dismissed")
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods", ""))
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")

    def test_history_still_rejects_post(self):
        status, _, _ = self._req("POST", "/history", {})
        self.assertEqual(status, 405)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ada/van && python3 -m unittest tests.test_serve_routes -v`
Expected: FAIL — `/dismissed` falls through to the static handler and returns 404.

- [ ] **Step 3: Implement in `/home/ada/van/bin/serve-data.py`**

In `Handler.__init__` add `self.no_store = False`. In `end_headers`, before `super().end_headers()`:
```python
        if self.no_store:
            self.send_header("Cache-Control", "no-store")
```
replacing the unconditional `max-age=60` line with an `else` branch, and add `POST` to `Access-Control-Allow-Methods`.

In `do_GET`, add before the static fall-through:
```python
        if u.path.rstrip("/") in ("/dismissed", "/dismissed.json"):
            self.no_store = True
            body = json.dumps({"ids": read_dismissed()}).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body)
            return
```
In `do_POST`, add before the `/history` branch:
```python
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
            if path == "/dismiss":
                if not isinstance(payload.get("id"), str) or not payload["id"]:
                    self._respond(400, "application/json; charset=utf-8",
                                  json.dumps({"error": "id required"}).encode("utf-8"))
                    return
                ids = dismiss_ids([payload["id"]])
                body = {"ok": True, "ids": ids}
            else:
                wanted = [i for i in (payload.get("ids") or []) if isinstance(i, str) and i]
                ids = dismiss_ids(wanted)
                body = {"ok": True, "count": len(wanted), "ids": ids}
            self.no_store = True
            self._respond(200, "application/json; charset=utf-8",
                          json.dumps(body).encode("utf-8"))
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ada/van && python3 -m unittest discover -s tests -v`
Expected: PASS (all suites).

- [ ] **Step 5: Commit**

```bash
cd /home/ada/van && git add bin/serve-data.py tests/test_serve_routes.py
git commit -m "van data: /dismissed, /dismiss, /dismiss_all with no-store + CORS POST"
```

---

### Task 4: Page-side pure helpers

**Files:**
- Modify: `/srv/van/index.html` (insert after `function fmtInt(...)`, before `// ---- zones`)
- Test: `/srv/van/tests/events.test.mjs`

**Interfaces:**
- Consumes: the event shape from Task 1 (`{id, ts, msg}`).
- Produces: `EVENT_WINDOW_S = 86400`, `EVENT_DISPLAY_MAX = 80`, `eventId(e) -> string`, `visibleEvents(events, now, dismissed, pending) -> {total: number, list: Array}`. `list` is oldest-first, capped to the newest `EVENT_DISPLAY_MAX`; `total` counts everything matching before the cap.

- [ ] **Step 1: Write the failing test**

```javascript
// /srv/van/tests/events.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const html = readFileSync("/srv/van/index.html", "utf8");
const start = html.indexOf("// ---- helpers");
const end = html.indexOf("// ---- zones");
assert.ok(start > -1 && end > start, "helpers block markers missing");
const src = html.slice(start, end);
const { eventId, visibleEvents, EVENT_DISPLAY_MAX } =
  new Function(src + "\nreturn { eventId, visibleEvents, EVENT_DISPLAY_MAX };")();

const NOW = 100000;
const ev = (ts, msg, id) => ({ ts, msg, id });

test("keeps events inside the 24h window", () => {
  const out = visibleEvents([ev(NOW - 86399, "a"), ev(NOW - 86401, "b")], NOW, {}, {});
  assert.deepEqual(out.list.map((e) => e.msg), ["a"]);
});

test("drops dismissed and pending ids", () => {
  const out = visibleEvents([ev(NOW - 10, "a", "i1"), ev(NOW - 9, "b", "i2")], NOW,
                            { i1: NOW - 5 }, { i2: NOW - 4 });
  assert.equal(out.total, 0);
});

test("falls back to ts when an event has no id", () => {
  assert.equal(eventId(ev(1234, "a")), "1234");
});

test("caps the list at the newest 80 but reports the true total", () => {
  const many = [];
  for (let i = 0; i < 95; i++) many.push(ev(NOW - 1000 + i, "m" + i, "id" + i));
  const out = visibleEvents(many, NOW, {}, {});
  assert.equal(out.total, 95);
  assert.equal(out.list.length, EVENT_DISPLAY_MAX);
  assert.equal(out.list[out.list.length - 1].msg, "m94");
});

test("returns events oldest-first", () => {
  const out = visibleEvents([ev(NOW - 5, "older", "a"), ev(NOW - 1, "newer", "b")], NOW, {}, {});
  assert.deepEqual(out.list.map((e) => e.msg), ["older", "newer"]);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /srv/van && node --test tests/events.test.mjs`
Expected: FAIL — `visibleEvents is not defined`.

- [ ] **Step 3: Implement the helpers in `/srv/van/index.html`**

Add the `Ada 02-Nov-2026` line to the file header comment, then insert immediately after the `fmtInt` line:
```js
// Ada 02-Nov-2026 alerts live 24h and are dismissible (Matt). These two are pure
// so tests/events.test.mjs can exercise them without a DOM.
var EVENT_WINDOW_S = 24 * 3600;
var EVENT_DISPLAY_MAX = 80;

function eventId(e) { return (e && e.id) ? e.id : String(Math.floor((e && e.ts) || 0)); }

function visibleEvents(events, now, dismissed, pending) {
  // oldest-first in, oldest-first out; newest EVENT_DISPLAY_MAX kept
}
```
`visibleEvents` filters on `e.ts && (now - e.ts) <= EVENT_WINDOW_S` and on the id being absent from both `dismissed` and `pending`, sets `total` to the pre-cap length, and slices with `slice(-EVENT_DISPLAY_MAX)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /srv/van && node --test tests/events.test.mjs`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
cd /srv/van && git add index.html tests/events.test.mjs
git commit -m "van page: pure alert helpers (24h window, 80-row cap) + node tests"
```

---

### Task 5: Render the scrollable, dismissible list

**Files:**
- Modify: `/srv/van/index.html` (`.masthead .events` CSS near line 125; the events block inside `load()` near line 1428; new delegated handlers near the existing info-circle handlers)

**Interfaces:**
- Consumes: `visibleEvents`, `eventId` (Task 4); the `/dismissed`, `/dismiss`, `/dismiss_all` routes (Task 3).
- Produces: DOM contract — `#events` contains one `div.evrow[data-id]` per alert plus, when capped, a muted `div.evmore`; a `button.evx[data-id]` per row; `button#evcloseall`.

- [ ] **Step 1: Add the CSS**

Replace the `.masthead .events` rule with a scroll container and row rules:
```css
.masthead .events{font-family:var(--font);font-size:.65rem;color:var(--dim);
  letter-spacing:.04em;margin:.25rem auto 0;display:block;max-width:min(100%,52rem);
  text-align:left;overflow-y:auto}
.masthead .evrow{display:flex;gap:.5rem;align-items:baseline;justify-content:space-between}
.masthead .evrow .evtxt{overflow-wrap:break-word}
.masthead .evx,.masthead #evcloseall{background:none;border:0;color:var(--faint);
  font:inherit;cursor:pointer;padding:0 .2rem}
.masthead .evmore{color:var(--faint);font-style:italic}
```
Set `max-height` on `.events` from the **measured** height of 20 rendered rows — measure in the browser at 20 rows rather than assuming, because the type scale has been scaled up twice. Record the measured value in the comment.

- [ ] **Step 2: Add the fetch and render**

Add a module-level `var dismissedIds = {};` and `var dismissedPending = {};`, plus:
```js
function loadDismissed() {
  return fetch("https://harlan.cinnamon-pinecone.ts.net:8891/dismissed")
    .then(function(r) { return r.json(); })
    .then(function(j) { dismissedIds = (j && j.ids) || {}; })
    .catch(function() {});
}
```
Call `loadDismissed()` once at startup and at the end of each `load()` tick (the endpoint is `no-store`, so every call is fresh). Replace the existing 15-minute render block with: compute `var vis = visibleEvents(snap.events || [], now, dismissedIds, dismissedPending);`, set `#events`'s `maxHeight` once, render one `div.evrow` per entry (`esc(age(e.ts) + " ago: " + e.msg)` inside `.evtxt`, then `<button class="evx" data-id="…">×</button>`), append a `div.evmore` reading `showing EVENT_DISPLAY_MAX of N` when `vis.total > vis.list.length`, and render a `close all` button **above the list** when the list is non-empty. `display:none` when the list is empty.

- [ ] **Step 3: Wire the handlers**

Delegated on `document` so they survive the 15 s re-render (same pattern as the info-circle tip toggle):
```js
document.addEventListener("click", function(ev) {
  var x = ev.target.closest && ev.target.closest(".evx");
  if (x) { dismissOne(x.getAttribute("data-id")); return; }
  if (ev.target.closest && ev.target.closest("#evcloseall")) { dismissAll(); }
});
```
`dismissOne(id)` sets `dismissedPending[id] = Date.now() / 1000`, re-renders, and POSTs to `/dismiss`. `dismissAll()` snapshots the currently rendered `data-id` values and POSTs them to `/dismiss_all`. Both clear their `dismissedPending` entries after the next successful `loadDismissed()`.

- [ ] **Step 4: Verify the JS parses and the tests still pass**

Run: `cd /srv/van && node --check <(sed -n '/^<script>$/,/^<\/script>$/p' index.html | sed '1d;$d') && node --test tests/events.test.mjs`
Expected: no syntax error; 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /srv/van && git add index.html
git commit -m "van page: 24h scrollable alerts with per-alert and close-all dismissal"
```

---

### Task 6: Deploy and verify live

**Files:**
- No file changes; operational verification.

- [ ] **Step 1: Restart the data server and the poller**

```bash
systemctl --user restart van-data.service && systemctl --user restart van-poll.service
systemctl --user is-active van-data.service van-poll.service
```
Expected: `active` for both.

- [ ] **Step 2: Confirm the routes and the no-store header**

```bash
curl -sS -D- -o /dev/null https://harlan.cinnamon-pinecone.ts.net:8891/dismissed | grep -iE "HTTP/|cache-control|access-control-allow-methods"
```
Expected: `200`, `cache-control: no-store`, `access-control-allow-methods` containing `POST`.

- [ ] **Step 3: Prove a dismissal survives a poller tick**

```bash
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"id":"verify-1"}' https://harlan.cinnamon-pinecone.ts.net:8891/dismiss
sleep 70                       # longer than one poll cycle
curl -sS https://harlan.cinnamon-pinecone.ts.net:8891/dismissed | grep -o 'verify-1'
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"ids":["verify-1"]}' https://harlan.cinnamon-pinecone.ts.net:8891/dismiss_all >/dev/null
```
Expected: `verify-1` still present after the sleep (the poller never touched the file). Clean it back up afterwards by deleting the id from `/srv/van/data/dismissed.json`.

- [ ] **Step 4: Confirm the page renders and the events survive 24h**

Hard-refresh `/van/index.html` and confirm: alerts render as rows, × removes one and it stays gone after 15 s, `close all` clears the list, and the block disappears when empty. Expect the list to be empty right after deploy (the store only holds new events) — trigger a change (e.g. a repeater hop) if a visible row is wanted for the check.

- [ ] **Step 5: Push note**

Per the van-page skill, `/srv/van` pushes first; `/home/ada/van` then needs a branch cherry-picked onto the updated `origin/main` and the token-bearing remote URL copied from `/srv/van/.git/config`. Do not rebase the initial commit.

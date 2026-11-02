# Van page — alert lifecycle (24 h retention, shared dismissal)

## Ada 02-Nov-2026 short design; written from Matt's decisions in session

## Intent and why

The `/van` page's state-change notifier currently lives for 15 minutes and then fades.
Matt wants it to behave like a log he can act on: alerts stay for **24 hours**, either of
the two humans can **close one** they've dealt with, and can **close all** at once. Closing
is shared: a dismissal made on one device is gone on the other.

## Goals

- Alerts persist 24 h from the event, not 15 min.
- Individual close (×) per alert, plus a "close all" that clears what's currently shown.
- Dismissal state is server-side and shared across devices/users (Hal included).
- Up to 20 alerts fit without a scrollbar; more scrolls; display hard-caps at **80**.
- A closed alert never comes back.

## Non-goals

- Not changing the six triggers (SSID, kmwan active, WAN port up/down, cellular, IMSI,
  dish stale/recovered) or their wording.
- Not adding severity levels, icons, or grouping.
- Not touching the dish alert bitmask row (that is `van-alerts-rendering`'s territory).
- Not making "flapping" itself an alert yet — see Open items.

## Anti-goals (working, but a failure)

- A dismissed alert reappears on the next 15 s render or after a reload.
- "Close all" swallows an alert that fired after the page rendered.
- The list grows unbounded and slows the 15 s render.
- Dismissals diverge between Matt's phone and Hal's screen.

## Constraints found in the code (as it works today)

- `poll.py` appends `{"ts", "msg"}` to `snap["events"]` on the six triggers and **caps at the
  newest 20** (`snap["events"] = events[-20:]`). snapshot.json is rewritten every tick from a
  copy read at the start of that tick.
- `/srv/van/index.html` renders `snap.events.filter(e => e.ts && now - e.ts <= 900)`, takes
  `slice(-5)`, oldest-first, via `textContent`; the block is inside `.masthead`, **outside
  `#dish`**, so the 15 s `#dish` re-render does not wipe it.
- `snapshot.json` is served with `cache-control: max-age=60`.
- Events have **no id** today — only `{ts, msg}`. `ts` is not unique: every trigger firing in
  one tick shares the same `now`.
- The `#events` div already exists with `.masthead .events` CSS.
- `van-data.service` runs with `ReadOnlyPaths=/srv/van/data`, deliberately: it can read the
  poller's files but must not write them. Any new writable state it owns has to live elsewhere,
  and a failed write must surface as an HTTP error rather than an empty reply.

## Approaches (Matt picked A in conversation)

- **A — separate `dismissed.json` owned by `van-data`.** Chosen. The poller never touches that
  file, so nothing can resurrect a closed alert; it is naturally shared.
- **B — dismissal list inside snapshot.json, carried forward by the poller.** Rejected: the
  poller rewrites snapshot.json every tick from a stale in-memory copy, so a dismiss arriving
  mid-tick is overwritten and the alert returns. Fixing it needs file locking.
- **C — browser localStorage.** Rejected: per-device, which contradicts the shared requirement.

## The design

**1. Event identity (poll.py).** Assign a stable `id` at append time:
`"%d-%d" % (int(now), seq)`, where `seq` counts events created within the same tick. On load,
normalise any existing entry that lacks an id to `str(int(ts))`. *My call:* old events in the
same second collide on that fallback and would dismiss together; acceptable for a 24 h window
of already-aged entries, and it disappears as the store turns over.

**2. Retention (poll.py).** Replace `[-20:]` with a time window and a safety cap:
keep events with `now - ts <= 26h`, then `[-500:]`. 26 h ≥ the 24 h the page shows, with
margin for clock skew between poller and browser. 500 stored keeps the file small while
sitting far above the 80 the page will ever display. *My call: both numbers.*

**3. Shared dismissal store (serve-data.py + `/home/ada/van/var/dismissed.json`).**
Shape `{"ids": {"<id>": <dismissed_ts>}}`. New routes on the existing `van-data` origin:

**Not under `/srv/van/data`** (where the snapshot lives), despite this being the obvious
neighbourhood: `van-data.service` runs with `ReadOnlyPaths=/srv/van/data` on purpose — its unit
comment says "this only reads it", because the poller is the writer there. The first live POST
against that path failed with `EROFS` and answered an *empty reply*. Relaxing the sandbox would
let a bug in the read server rewrite `van.db`, so the store lives in `/home/ada/van/var/`
instead: no unit change, and the poller's data stays read-only to this service. **A write
failure must answer 500 JSON, never an empty reply** — an empty reply is what made this look
like "dismiss does nothing" from the browser.

- `GET /dismissed` → the id map. **Must be served `cache-control: no-store`** — snapshot.json's
  60 s cache would otherwise hand back pre-dismissal state and the alert would flash back on
  the next fetch. This is the single most likely way to break the anti-goals.
- `POST /dismiss` with `{"id": "..."}`.
- `POST /dismiss_all` with `{"ids": [...]}` — the ids **currently rendered**, sent by the page.
  *My call:* not "everything in the window", so an alert that fired between render and click
  is not silently swallowed.

Writes are atomic (temp file + `os.replace`). Prune ids older than 26 h on write. The poller
never opens this file, so there is no race to lock.

**4. Rendering (`/srv/van/index.html`).** Fetch `/dismissed` on load and on each `load()` tick
alongside snapshot.json. Then:
`visible = events.filter(within 24h && !dismissed && !pending)` , oldest-first.
Render all of `visible`, capped at the **newest 80**. Wrap the list in a scroll container with
a `max-height` equal to the height of 20 rows — so 1–20 shows no scrollbar, 21–80 scrolls, and
past 80 a muted "showing 80 of N" line appears. Each row gets an × that POSTs `/dismiss`; a
"close all" control POSTs `/dismiss_all` with the rendered ids. Both hide optimistically via
the local `pending` set, cleared once the next `/dismissed` fetch confirms — that is what stops
a 15 s re-render from flashing a just-closed row back. Handlers are **delegated on `document`**
(they survive the re-render) — same pattern the existing info-circle tip toggle uses.

**5. Sizes and limits.** ≤20 rows: no scrollbar. 21–80: scrollbar. >80 in 24 h: capped display
plus the muted count line.

## What's left to the builder

- Exact `max-height` (derive from the measured row height at 20 rows; the type scale has been
  scaled up twice, so measure, don't assume).
- Whether the × and "close all" are text glyphs or buttons (must stay legible at the page's
  small type size and not wrap the row).
- Where to place "close all" so it does not collide with the stale badge.
- The scroll container must not break the masthead's centred layout
  (`.masthead .events` is `display:inline-block; text-align:left`).

## Open items

- **Matt's flapping hunch.** 80 events in 24 h almost certainly means something is oscillating.
  The capped-display line makes that visible on the page; turning it into an actual alert
  (e.g. "N state changes in the last hour") is a separate change and is **not** in this design.
- The six triggers will produce more events per day than the old 20-entry cap tolerated, which
  is what makes this change necessary in the first place.

---

## Appendix: decisions taken during implementation (02-Nov-2026)

Recorded here because the plan's scratch ledger is deleted once the work lands. Each was a
deviation from the plan or a call the plan left open; each is in the commit message that made it.

**Plan defects found by the tests (fixed, not worked around):**
- `record_events` now normalises on the way in. The plan had the call site normalise outside it,
  but the plan's own Interfaces block promises `{id, ts, msg}` output — enforcing it where the
  type is produced means a caller cannot emit an id-less, undismissable event.
- The dismissal tests load `bin/serve-data.py` by path (`importlib.spec_from_file_location`)
  because the filename has a hyphen and `import serve_data` cannot work.
- The JS test's marker slice is **line-anchored** (`\n// ---- helpers`). A bare `indexOf` matched
  a doc comment that quoted both markers, collapsing the slice to 13 lines.
- `no_store` is a **class** attribute. `BaseRequestHandler.__init__` calls `handle()` itself, so
  anything set after `super().__init__()` is too late and every response raised `AttributeError`.
- The plan's store tests would have written to the live store: `path=DISMISSED` as a default
  argument binds at import time, so a patched module global would not redirect it. Now `path=None`
  with the lookup inside the body.

**Calls the plan left to the builder:**
- `age(ts, now)` hoisted out of `load()` to a top-level function, so the renderer can run from a
  dismissal as well as from a tick. Its three call sites pass `now`.
- `renderEvents(snap, now)` takes the snapshot explicitly — the page has no global `SNAP` — and
  `load()` stashes it in `lastSnap` for dismissal-time repaints.
- The close-all control lives in a new `#evbar` **outside** the scroll box, so the 20-row budget
  stays 20 alerts rather than 19 plus a button.
- Row height measured, not assumed: 15.61 px in a 10.4 px font, so 20 rows = 312.19 px and
  `max-height:19.6rem` (313.6 px) holds 20 and scrolls at 21.
- An optimistically hidden id stays in `dismissedPending` until the server's list actually
  contains it, so a slow POST cannot flash the alert back.

**Constraint discovered only by deploying (see the §3 note):** the store cannot live under
`/srv/van/data`; it is at `/home/ada/van/var/dismissed.json`.

**Post-review fixes** (a fresh reviewer found a race the plan's tests could not):
- The dismissal read-modify-write is now under a `threading.Lock` and writes to a unique temp
  file (`tempfile.mkstemp`). Without it, the threaded server + un-awaited POSTs from the page lose
  dismissals — the reviewer measured 277 of 320 lost, and the lost ids come back, which is a
  written anti-goal here.
- The page now checks `r.ok` on a dismissal POST. It used to swallow the 500 the server was built
  to send, so a refused dismissal left the row hidden until a reload and then returned
  unexplained. A refusal un-hides the rows and says so.

**Deferred minors** (reviewer-flagged, deliberately not fixed): `esc()` does not escape quotes for
the `data-id` attribute; `seq` resets each tick so two ticks in one second would collide on id;
`normalise_events` raises on a non-dict entry; the JS suite does not cover `renderEvents` or the
handlers; the PLAN doc still names the old store path; `Access-Control-Allow-Origin: *` plus
unauthenticated write routes allows CSRF-style alert-hiding from any tailnet origin; `19.6rem` is
a magic number.

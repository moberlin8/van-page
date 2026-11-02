// ## Ada 02-Nov-2026 tests for the /van alert helpers.
//
// The helpers are pure and DOM-free so they can be exercised here: the test
// slices the source between the `// ---- helpers` and `// ---- zones` markers in
// the served page, which is the region those functions are required to live in.
// Anything with a top-level side effect (module state, a document handler) must
// stay OUTSIDE that slice or this file evaluates it and throws.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const html = readFileSync("/srv/van/index.html", "utf8");
// Line-anchored on purpose: a prose mention of a marker inside a comment would
// otherwise truncate the slice (that happened: a doc comment quoting both markers
// made the slice 13 lines long and the helpers invisible to the test).
const start = html.indexOf("\n// ---- helpers");
const end = html.indexOf("\n// ---- zones");
assert.ok(start > -1 && end > start, "helpers block markers missing");
const src = html.slice(start, end);
const { eventId, visibleEvents, EVENT_DISPLAY_MAX, EVENT_WINDOW_S } =
  new Function(src + "\nreturn { eventId, visibleEvents, EVENT_DISPLAY_MAX, EVENT_WINDOW_S };")();

const NOW = 100000;
const ev = (ts, msg, id) => ({ ts, msg, id });

test("the window is 24h", () => {
  assert.equal(EVENT_WINDOW_S, 24 * 3600);
});

test("keeps events inside the 24h window", () => {
  const out = visibleEvents([ev(NOW - 86399, "a"), ev(NOW - 86401, "b")], NOW, {}, {});
  assert.deepEqual(out.list.map((e) => e.msg), ["a"]);
  assert.equal(out.total, 1);
});

test("drops dismissed and pending ids", () => {
  const out = visibleEvents([ev(NOW - 10, "a", "i1"), ev(NOW - 9, "b", "i2")], NOW,
                            { i1: NOW - 5 }, { i2: NOW - 4 });
  assert.equal(out.total, 0);
  assert.deepEqual(out.list, []);
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
  assert.equal(out.list[0].msg, "m15");
});

test("returns events oldest-first", () => {
  const out = visibleEvents([ev(NOW - 5, "older", "a"), ev(NOW - 1, "newer", "b")], NOW, {}, {});
  assert.deepEqual(out.list.map((e) => e.msg), ["older", "newer"]);
});

test("an event with no ts is dropped, not rendered as an age of NaN", () => {
  const out = visibleEvents([{ msg: "no ts", id: "x" }], NOW, {}, {});
  assert.equal(out.total, 0);
});

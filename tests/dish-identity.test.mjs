// ## Ada 09-Nov-2026 the /van dish panel's identity gate.
//
// Matt: "gate the dish panel on EvilDish's identity so the home dish can't appear there", and
// "ut01188c8b- is the big boy dish". Two Starlink kits answer 192.168.100.1, so the panel follows
// whichever wins the route, and that has been the home kit since 05-Nov. `dishRefusal` is the
// page's half of the gate: it returns null when the reading may be rendered and a reason string
// when it may not, so the panel cannot be painted around the check by accident.
//
// The pure helpers live between the `// ---- helpers` and `// ---- zones` markers; this test
// slices that region and runs it with no DOM, per references/testing.md.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HTML = path.join(HERE, "..", "index.html");

function helpers() {
  const src = fs.readFileSync(HTML, "utf8");
  // Anchor to a line start: a bare indexOf would also match a prose mention of the marker.
  const start = src.indexOf("\n// ---- helpers");
  const end = src.indexOf("\n// ---- zones");
  assert.ok(start >= 0 && end > start, "the helpers region markers moved");
  const body = src.slice(start, end);
  // A collapsed slice would make every assertion below vacuous.
  assert.ok(body.length > 500, "the helpers slice collapsed to " + body.length + " bytes");
  const out = new Function(body + "\nreturn { dishRefusal: dishRefusal };")();
  assert.equal(typeof out.dishRefusal, "function", "dishRefusal is not in the helpers region");
  return out;
}

const { dishRefusal } = helpers();

test("the van's own terminal renders", () => {
  assert.equal(dishRefusal({ van_terminal: true, id: "ut-van-0001" }), null);
});

test("a known other kit is refused, and named", () => {
  const why = dishRefusal({ van_terminal: false, id: "ut01188c8b-8281880f-1ac2a7ee" });
  assert.ok(why, "the home kit must be refused");
  assert.match(why, /different Starlink kit/);
  assert.match(why, /ut01188c8b-8281880f-1ac2a7ee/);
});

test("no identity reported is refused, not assumed", () => {
  assert.match(dishRefusal({ van_terminal: null, id: null }), /no identity/);
});

test("a snapshot written before the gate existed cannot leak the other kit", () => {
  // van_terminal absent entirely: anything but an explicit true is refused
  const why = dishRefusal({ id: "ut01188c8b-8281880f-1ac2a7ee", state: "CONNECTED" });
  assert.ok(why, "an absent verdict must refuse");
  assert.doesNotMatch(why, /different Starlink kit/);   // it is not yet a known-other, just unknown
});

test("no dish at all says so", () => {
  assert.match(dishRefusal(null), /no dish data/);
  assert.match(dishRefusal(undefined), /no dish data/);
});

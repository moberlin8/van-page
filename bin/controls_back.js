function doReboot() {
  const btn = document.getElementById("reboot-btn");
  const status = document.getElementById("reboot-status");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "rebooting";
  status.textContent = "requesting…";
  fetch("/reboot", {method: "POST"})
    .then(function (r) { return r.json(); })
    .then(function (body) {
      if (body && body.ok) {
        status.textContent = "accepted — dish rebooting (1&ndash;3 min)";
        status.className = "ok";
      } else {
        status.textContent = (body && body.error) || "reboot failed";
        status.className = "warn";
        btn.disabled = false;
        btn.textContent = "Reboot dish";
      }
    })
    .catch(function () {
      status.textContent = "Reboot request failed";
      status.className = "warn";
      btn.disabled = false;
      btn.textContent = "Reboot dish";
    });
}

async function load() {
  let snap = null;
  try {
    // port 8891 is the van-data JSON server; Next.js would 404 this path
    const r = await fetch("https://harlan.cinnamon-pinecone.ts.net:8891/snapshot.json?t="
      + Date.now(), {cache: "no-store"});
    snap = await r.json();
  } catch (e) {
    $("#stalemark").innerHTML = '<span class="stale">cannot read snapshot.json</span>';
    $("#dish").innerHTML = '<h2>Starlink EvilDish</h2><p class="muted">'
      + esc(String(e).slice(0, 200)) + "</p>";
    return;
  }

  const now = Date.now() / 1000;
  // Ada 24-Oct-2026 the ages moved onto the masthead's two device lines (Matt):
  // "Starlink EvilDish 57s" / "Delilah Router 57s". The old #when line is gone.
  const age = (ts) => ts ? Math.round(now - ts) + "s" : "&mdash;";
  const agoDish = $("#ago-dish"), agoRouter = $("#ago-router");
  if (agoDish) agoDish.textContent = age(snap.dish_ts);
  if (agoRouter) agoRouter.textContent = age(snap.delilah_ts);

  const stale = [];
  if (snap.dish_stale || !snap.dish) stale.push("dish");
  if (snap.delilah_stale || !snap.dish) stale.push("router");
  $("#stalemark").innerHTML = stale.length
    ? '<span class="stale">stale: ' + esc(stale.join(", ")) + '</span>' : "";
  // device controls panel: separate from #dish (which re-renders every 15s), so
  // it survives the tick and picks up the fresh config on the next poll.

  // Ada 24-Oct-2026 load() re-renders ALL of #dish every 15s, which wiped the
  // throughput tiles (filled by loadHistory(), every 60s) and the usage numbers
  // (filled by loadUsage(), every 60s). They then sat blank for up to 45s, so
  // the boxes visibly disappeared and came back -- a loop. Snapshot their
  // content and put it straight back after the re-render.
  const keep = {};
  for (const el of [$("#tput"), $("#usg-down"), $("#usg-up"), $("#usg-total"),
                   $("#usg-period"), $("#usg-days"), $("#usg-since")]) {
    if (el) keep[el.id] = el.innerHTML;
  }
  $("#dish").innerHTML = snap.dish
    ? dishSection(snap.dish)
    : '<h2>Starlink EvilDish</h2><p class="muted">no dish data yet &mdash; '
      + esc(snap.dish_error || "tunnel down") + "</p>";
  for (const id in keep) {
    const el = $(id);
    if (el) el.innerHTML = keep[id];
  }

  $("#router").innerHTML = snap.delilah
    ? routerSection(snap.delilah)
    : '<h2>Delilah Router</h2><p class="muted">no router data yet &mdash; '
      + esc(snap.delilah_error || "tunnel down") + "</p>";
}

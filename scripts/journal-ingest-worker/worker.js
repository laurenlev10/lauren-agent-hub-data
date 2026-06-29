/* ===========================================================================
   Journal Ingest Worker v2 — receives TradingView alerts from the 3 strategy
   indicators (BASE / PARTIAL / MOMENTUM) and writes closed trades to
   journal-data.json, tagged by strategy (taken from the URL path).
   Route: POST https://<worker>/<strategy>   strategy = base | partial | momentum

   v2 (2026-06-29):
   - Responds 200 INSTANTLY and does the GitHub read-merge-write in the
     background via ctx.waitUntil() -> never hits TradingView's webhook timeout
     (this was the cause of the red "request took too long and timed out").
   - Accepts the per-strategy "all events" alert format:
       { strategy, dir:1|-1, event:1..5, qty, price }
       event: 1=entry 2=TP 3=SL 4=SWAP 5=TIME/NEWS  (momentum: 1=entry 2=exit)
     Still accepts the legacy { action:buy|sell|exit, type, price } format.
   - Strategy comes from the URL path (works even if the message hardcodes a
     different "strategy" field, e.g. PARTIAL file firing the BASE condition).
   Required secret: GH_TOKEN (Contents:write on the repo).
   =========================================================================== */
const REPO = "laurenlev10/lauren-agent-hub-data";
const JOURNAL = "docs/trading/journal-data.json";
const TICK = 0.25;            // MNQ tick size (price)
const DOLLAR_PER_TICK = 0.5;  // MNQ $ per tick

export default {
  async fetch(request, env, ctx) {
    const cors = { "Access-Control-Allow-Origin":"*", "Access-Control-Allow-Methods":"POST, OPTIONS", "Access-Control-Allow-Headers":"Content-Type" };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    const url = new URL(request.url);
    const m = url.pathname.match(/\/(base|partial|momentum)\/?$/i);
    if (request.method !== "POST") return reply({ error:"POST only" }, 405, cors);
    if (!m) return reply({ error:"path must end with /base, /partial or /momentum" }, 404, cors);
    const strategy = m[1].toLowerCase();
    const TOKEN = (env.GH_TOKEN || "").trim();
    if (!TOKEN) return reply({ error:"server not configured (no GH_TOKEN)" }, 500, cors);

    let p;
    try { p = await request.json(); }
    catch (e) { try { p = JSON.parse(await request.text()); } catch (e2) { return reply({ error:"bad json" }, 400, cors); } }

    // Acknowledge instantly; do the slow GitHub write in the background.
    ctx.waitUntil(processAlert(TOKEN, strategy, p));
    return reply({ ok:true, queued:true, strategy }, 200, cors);
  }
};

async function processAlert(TOKEN, strategy, p) {
  const now   = new Date();
  const price = parseFloat(p.price);
  const hasEvent = p.event !== undefined && p.event !== null && p.event !== "";
  const ev  = hasEvent ? Number(p.event) : null;
  const dir = (p.dir !== undefined && p.dir !== null && p.dir !== "") ? Number(p.dir) : null;
  const action = String(p.action || "").toLowerCase();
  const type   = String(p.type   || "").toUpperCase();

  for (let attempt = 0; attempt < 8; attempt++) {
    const cur  = await ghGet(TOKEN, JOURNAL);
    const data = cur.json || { trades: [] };
    data.trades = data.trades || [];
    data._open  = data._open  || {};
    const open  = data._open[strategy] || null;
    let changed = true;

    if (hasEvent) {
      // ---- per-strategy "all events" format ----
      if (ev === 1) {                                   // entry
        data._open[strategy] = mkOpen(dir, price, now);
      } else if (ev === 4) {                            // SWAP: close old, open new
        if (open && !isNaN(price)) pushClosed(data, strategy, open, price, "SWAP", now);
        data._open[strategy] = mkOpen(dir, price, now);
      } else if (ev === 2 || ev === 3 || ev === 5) {    // exit (TP/SL/TIME)
        if (open) {
          const d  = open.dir === "long" ? 1 : -1;
          const tk = Math.round((price - open.entry_price) * d / TICK);
          let reason = ev === 3 ? "SL" : ev === 5 ? "TIME" : "TP";
          if (ev === 2 && !isNaN(tk) && tk < 0) reason = "SL"; // momentum: single exit event
          pushClosed(data, strategy, open, isNaN(price) ? open.entry_price : price, reason, now);
          delete data._open[strategy];
        } else changed = false;
      } else changed = false;
    } else if (action === "buy" || action === "sell") {
      // ---- legacy execution format ----
      if (open && !isNaN(price)) pushClosed(data, strategy, open, price, "SWAP", now);
      data._open[strategy] = mkOpen(action === "buy" ? 1 : -1, price, now);
    } else if (action === "exit") {
      if (open) {
        const reason = type.includes("TP") ? "TP" : type.includes("SL") ? "SL"
                     : type.includes("SWAP") ? "SWAP" : type.includes("BE") ? "BE"
                     : (type.includes("TIME") || type === "") ? "TIME" : "EXIT";
        pushClosed(data, strategy, open, isNaN(price) ? open.entry_price : price, reason, now);
        delete data._open[strategy];
      } else changed = false;
    } else changed = false;

    if (!changed) return;  // nothing actionable

    data._updated_at = now.toISOString();
    const tag = hasEvent ? ("ev" + ev) : action;
    const ok = await ghPut(TOKEN, JOURNAL, data, cur.sha, ("journal: " + strategy + " " + tag).trim());
    if (ok) return;
    await sleep(200 + Math.random()*500);   // write conflict -> back off, re-read, retry
  }
  // give up silently; the hourly broker reconciler is the backstop
}

function mkOpen(dir, price, now) {
  return { dir: dir === 1 ? "long" : "short", entry_price: price, entry_iso: now.toISOString(), entry_la: laStr(now) };
}
function pushClosed(data, strategy, open, exitPrice, reason, now) {
  const dir = open.dir === "long" ? 1 : -1;
  const ticks = Math.round((exitPrice - open.entry_price) * dir / TICK);
  data.trades.push({
    id: data.trades.length + 1, ticker:"MNQ", strategy, direction: open.dir,
    result_type: reason, result_ticks: ticks, result_dollars: Math.round(ticks * DOLLAR_PER_TICK * 100)/100,
    _entry_price: open.entry_price, _exit_price: exitPrice,
    _entry_time_la: open.entry_la, _received_at: now.toISOString(),
    _source:"indicator-webhook", _data_origin:"live"
  });
}
function laStr(d) {
  const f = new Intl.DateTimeFormat("en-GB", { timeZone:"America/Los_Angeles", day:"2-digit", month:"2-digit", year:"numeric", hour:"2-digit", minute:"2-digit", hour12:false });
  const o = {}; for (const x of f.formatToParts(d)) o[x.type] = x.value;
  return o.day + "/" + o.month + "/" + o.year + " " + o.hour + ":" + o.minute;
}
function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
function reply(obj, status, cors){ return new Response(JSON.stringify(obj), { status, headers: { "Content-Type":"application/json", ...cors } }); }
async function ghGet(token, path){
  const r = await fetch("https://api.github.com/repos/" + REPO + "/contents/" + path + "?ref=main&_=" + Date.now(),
    { headers:{ "Authorization":"token "+token, "Accept":"application/vnd.github+json", "User-Agent":"journal-ingest" }, cf:{ cacheTtl:0 } });
  if (!r.ok) return { json:null, sha:null };
  const j = await r.json();
  let content = null;
  try { content = JSON.parse(decodeURIComponent(escape(atob((j.content||"").replace(/\n/g,""))))); } catch(e){ content = null; }
  return { json: content, sha: j.sha };
}
async function ghPut(token, path, obj, sha, msg){
  const body = { message: msg, content: btoa(unescape(encodeURIComponent(JSON.stringify(obj, null, 1)))), branch:"main" };
  if (sha) body.sha = sha;
  const r = await fetch("https://api.github.com/repos/" + REPO + "/contents/" + path,
    { method:"PUT", headers:{ "Authorization":"token "+token, "Accept":"application/vnd.github+json", "Content-Type":"application/json", "User-Agent":"journal-ingest" }, body: JSON.stringify(body) });
  return r.ok;
}

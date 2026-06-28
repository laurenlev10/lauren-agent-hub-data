/* ===========================================================================
   Journal Ingest Worker — receives TradingView alerts straight from the 3
   strategy indicators (BASE / PARTIAL / MOMENTUM) and writes closed trades to
   journal-data.json, tagged by strategy. No Zapier, no broker — pure data.
   Route: POST https://<worker>/<strategy>   (strategy = base | partial | momentum)
   Each strategy's TradingView alert ("Any alert() function call") posts here.
   Pairs entry+exit per strategy, computes ticks/$ from prices. Holds GH_TOKEN
   server-side. Required secret: GH_TOKEN (Contents:write on the repo).
   =========================================================================== */
const REPO = "laurenlev10/lauren-agent-hub-data";
const JOURNAL = "docs/trading/journal-data.json";
const TICK = 0.25;      // MNQ tick size (price)
const DOLLAR_PER_TICK = 0.5;

export default {
  async fetch(request, env) {
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

    const action = String(p.action || "").toLowerCase();
    const type   = String(p.type   || "").toUpperCase();
    const price  = parseFloat(p.price);
    const now    = new Date();

    for (let attempt = 0; attempt < 5; attempt++) {
      const cur = await ghGet(TOKEN, JOURNAL);
      const data = cur.json || { trades: [] };
      data.trades = data.trades || [];
      data._open  = data._open  || {};   // per-strategy open trade state (not a trade row)
      const open = data._open[strategy] || null;

      if (action === "buy" || action === "sell") {
        // SWAP: if an opposite trade is open, close it at this price first
        if (open && !isNaN(price)) pushClosed(data, strategy, open, price, "SWAP", now);
        data._open[strategy] = { dir: action === "buy" ? "long" : "short", entry_price: price,
                                 entry_iso: now.toISOString(), entry_la: laStr(now) };
      } else if (action === "exit") {
        if (open) {
          const reason = type.includes("TP") ? "TP" : type.includes("SL") ? "SL"
                       : type.includes("SWAP") ? "SWAP" : type.includes("BE") ? "BE"
                       : (type.includes("TIME") || type === "") ? "TIME" : "EXIT";
          pushClosed(data, strategy, open, isNaN(price) ? open.entry_price : price, reason, now);
          delete data._open[strategy];
        }
      } else { return reply({ ok:true, ignored:action }, 200, cors); }

      data._updated_at = now.toISOString();
      const ok = await ghPut(TOKEN, JOURNAL, data, cur.sha, `journal: ${strategy} ${action} ${type||""}`.trim());
      if (ok) return reply({ ok:true, strategy, action, closed: data.trades.length }, 200, cors);
      await sleep(250 + Math.random()*400);
    }
    return reply({ error:"write conflict after retries" }, 503, cors);
  }
};

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
  return `${o.day}/${o.month}/${o.year} ${o.hour}:${o.minute}`;
}
function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
function reply(obj, status, cors){ return new Response(JSON.stringify(obj), { status, headers: { "Content-Type":"application/json", ...cors } }); }
async function ghGet(token, path){
  const r = await fetch(`https://api.github.com/repos/${REPO}/contents/${path}?ref=main&_=${Date.now()}`,
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
  const r = await fetch(`https://api.github.com/repos/${REPO}/contents/${path}`,
    { method:"PUT", headers:{ "Authorization":"token "+token, "Accept":"application/vnd.github+json", "Content-Type":"application/json", "User-Agent":"journal-ingest" }, body: JSON.stringify(body) });
  return r.ok;
}

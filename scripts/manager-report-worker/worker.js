/* ===========================================================================
   Manager Report — Cloud Save Worker (Cloudflare)
   ---------------------------------------------------------------------------
   Holds the GitHub token SERVER-SIDE so the manager's phone never sees it.
   The form POSTs the filled report (and photos) here; this Worker commits it
   to the repo, so it shows up in Lauren's dashboard automatically.

   Deploy: see manager-report-DEPLOY-GUIDE.md
   Required secret:  GH_TOKEN  = a GitHub token with "Contents: write" on the
                                 laurenlev10/lauren-agent-hub-data repo.
   =========================================================================== */

const REPO  = "laurenlev10/lauren-agent-hub-data";
const STATE = "docs/state/manager_reports.json";
const SITE  = "https://dashboard.themakeupblowout.com";   // GitHub Pages custom domain

export default {
  async fetch(request, env) {
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type"
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    if (request.method !== "POST")    return reply({ error: "POST only" }, 405, cors);

    const TOKEN = (env.GH_TOKEN || "").trim();   // trim so a trailing newline/space in the secret can't cause 401
    if (!TOKEN) return reply({ error: "server not configured (no GH_TOKEN)" }, 500, cors);

    let report;
    try { report = await request.json(); }
    catch (e) { return reply({ error: "bad json" }, 400, cors); }

    // --- Team availability form (docs/team/availability.html) ---------------
    // Routed by kind, saved to its own state file so manager_reports stays clean.
    if (report.kind === "team_availability") {
      const AVAIL = "docs/state/team_availability.json";
      for (let attempt = 0; attempt < 4; attempt++) {
        const cur = await ghGet(TOKEN, AVAIL);
        const data = cur.json || { _updated_at: null, submissions: [] };
        if (!Array.isArray(data.submissions)) data.submissions = [];
        delete report.photos;
        data.submissions.push(report);
        data._updated_at = new Date().toISOString();
        const res = await ghPutJson(TOKEN, AVAIL, data, cur.sha,
          `team-availability: ${report.name || report.member_slug || "?"}`);
        if (res === true) return reply({ ok: true, kind: "team_availability" }, 200, cors);
        if (res !== 409)  return reply({ error: "github write failed " + res }, 502, cors);
      }
      return reply({ error: "conflict retries exhausted" }, 502, cors);
    }

    const rawKey = report.evkey || "unknown";
    const safeKey = String(rawKey).replace(/[^a-z0-9\-]/gi, "-").toLowerCase();
    const ts = new Date().toISOString().replace(/[:.]/g, "-");

    // 1) Commit photos (if any) and collect their public URLs.
    const photoUrls = [];
    const photos = Array.isArray(report.photos) ? report.photos.slice(0, 8) : [];
    for (let i = 0; i < photos.length; i++) {
      const b64 = String(photos[i].data || "").split(",").pop();   // strip "data:image/jpeg;base64,"
      if (!b64) continue;
      const path = `docs/state/manager-report-photos/${safeKey}/${ts}-${i}.jpg`;
      const ok = await ghPutB64(TOKEN, path, b64, `manager-report photo ${safeKey} ${ts}-${i}`);
      if (ok) photoUrls.push(`${SITE}/state/manager-report-photos/${safeKey}/${ts}-${i}.jpg`);
    }
    delete report.photos;                       // never store the heavy base64
    if (photoUrls.length) report.photos = photoUrls;

    // 2) Save to manager_reports.json (read-modify-write, retry on conflict).
    //    A "draft" overwrites a single per-event draft slot; a final report appends
    //    to reports[evkey] and clears any draft for that event.
    const isDraft = report.mode === "draft";
    for (let attempt = 0; attempt < 4; attempt++) {
      const cur = await ghGet(TOKEN, STATE);
      const data = cur.json || { _updated_at: null, reports: {}, drafts: {} };
      if (!data.reports) data.reports = {};
      if (!data.drafts) data.drafts = {};
      if (isDraft) {
        data.drafts[rawKey] = report;                 // one draft per event (overwrite)
      } else {
        const arr = data.reports[rawKey] || [];
        arr.push(report);
        data.reports[rawKey] = arr;
        delete data.drafts[rawKey];                   // final submit clears the draft
      }
      data._updated_at = new Date().toISOString();
      const res = await ghPutJson(TOKEN, STATE, data, cur.sha,
        `manager-report ${isDraft ? "draft" : "final"}: ${rawKey} — ${report.manager_name || ""}`);
      if (res === true) return reply({ ok: true, draft: isDraft, photos: photoUrls.length }, 200, cors);
      if (res !== 409)  return reply({ error: "github write failed " + res }, 502, cors);
      // 409 = someone else committed; loop and retry with fresh sha
    }
    return reply({ error: "conflict retries exhausted" }, 502, cors);
  }
};

/* ---------- helpers ---------- */
function reply(obj, status, cors) {
  return new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });
}
const API = "https://api.github.com/repos/";
const HDR = (t) => ({
  "Authorization": "token " + t,
  "Accept": "application/vnd.github+json",
  "User-Agent": "mbs-manager-report",
  "Content-Type": "application/json"
});

async function ghGet(token, path) {
  const r = await fetch(API + REPO + "/contents/" + path + "?ref=main", { headers: HDR(token) });
  if (!r.ok) return { sha: null, json: null };
  const j = await r.json();
  let content = null;
  try { content = JSON.parse(b64ToStr(j.content)); } catch (e) {}
  return { sha: j.sha, json: content };
}
async function ghPutJson(token, path, obj, sha, msg) {
  const body = { message: msg, content: strToB64(JSON.stringify(obj, null, 2)), branch: "main" };
  if (sha) body.sha = sha;
  const r = await fetch(API + REPO + "/contents/" + path, { method: "PUT", headers: HDR(token), body: JSON.stringify(body) });
  return r.ok ? true : r.status;
}
async function ghPutB64(token, path, b64Content, msg) {
  const r = await fetch(API + REPO + "/contents/" + path, {
    method: "PUT", headers: HDR(token),
    body: JSON.stringify({ message: msg, content: b64Content, branch: "main" })
  });
  return r.ok;
}
function strToB64(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = ""; for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}
function b64ToStr(b64) {
  const bin = atob(String(b64).replace(/\n/g, ""));
  const bytes = Uint8Array.from(bin, c => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

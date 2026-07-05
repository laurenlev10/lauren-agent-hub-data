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

    // --- Influencer application (events.themakeupblowout.com/collab/) -------
    // Inbound "work with us" applications. Appended to their own state file.
    if (report.kind === "influencer_application") {
      if (report.website) return reply({ ok: true }, 200, cors);   // honeypot -> pretend success, save nothing
      const APPS = "docs/state/influencer_applications.json";
      const app = {
        received_at: new Date().toISOString(),
        full_name: String(report.full_name || "").slice(0, 120),
        handle: String(report.handle || "").slice(0, 80),
        platform: String(report.platform || "").slice(0, 40),
        followers: String(report.followers || "").slice(0, 40),
        city: String(report.city || "").slice(0, 120),
        email: String(report.email || "").slice(0, 160),
        phone: String(report.phone || "").slice(0, 40),
        event: String(report.event || "").slice(0, 160),
        about: String(report.about || "").slice(0, 600)
      };
      for (let attempt = 0; attempt < 4; attempt++) {
        const cur = await ghGet(TOKEN, APPS);
        const data = cur.json || { _updated_at: null, applications: [] };
        if (!Array.isArray(data.applications)) data.applications = [];
        data.applications.push(app);
        data._updated_at = new Date().toISOString();
        const res = await ghPutJson(TOKEN, APPS, data, cur.sha,
          `influencer-application: ${app.handle || app.full_name || "?"}`);
        if (res === true) return reply({ ok: true, kind: "influencer_application" }, 200, cors);
        if (res !== 409)  return reply({ error: "github write failed " + res }, 502, cors);
      }
      return reply({ error: "conflict retries exhausted" }, 502, cors);
    }

    // --- Influencer collab brief (events.themakeupblowout.com/influencer-brief/) ---
    // Routed by kind, saved to its own state file + signature PNG committed separately.
    if (report.kind === "influencer_brief") {
      const BRIEFS = "docs/state/influencer_briefs.json";
      const bKeyRaw = report.evkey || "unknown";
      const bKey = String(bKeyRaw).replace(/[^a-z0-9\-]/gi, "-").toLowerCase();
      const bTs = new Date().toISOString().replace(/[:.]/g, "-");
      // signature image -> its own file (never store heavy base64 in the JSON)
      let sigUrl = null;
      const sigB64 = String(report.signature || "").split(",").pop();
      if (sigB64 && sigB64.length > 100) {
        const spath = `docs/state/influencer-brief-signatures/${bKey}/${bTs}.png`;
        const ok = await ghPutB64(TOKEN, spath, sigB64, `influencer-brief signature ${bKey} ${bTs}`);
        if (ok) sigUrl = `${SITE}/state/influencer-brief-signatures/${bKey}/${bTs}.png`;
      }
      delete report.signature;
      delete report.photos;
      if (sigUrl) report.signature_url = sigUrl;
      report.received_at = new Date().toISOString();
      for (let attempt = 0; attempt < 4; attempt++) {
        const cur = await ghGet(TOKEN, BRIEFS);
        const data = cur.json || { _updated_at: null, briefs: {} };
        if (!data.briefs) data.briefs = {};
        const arr = data.briefs[bKeyRaw] || [];
        arr.push(report);
        data.briefs[bKeyRaw] = arr;
        data._updated_at = new Date().toISOString();
        const res = await ghPutJson(TOKEN, BRIEFS, data, cur.sha,
          `influencer-brief: ${report.handle || report.full_name || "?"} @ ${bKeyRaw}`);
        if (res === true) return reply({ ok: true, kind: "influencer_brief", signature_saved: !!sigUrl }, 200, cors);
        if (res !== 409)  return reply({ error: "github write failed " + res }, 502, cors);
      }
      return reply({ error: "conflict retries exhausted" }, 502, cors);
    }

    // --- Recount counting form (events.themakeupblowout.com/recount-count/) ---
    // Set 2026-07-05. OCTOPOS can't report exact-match (zero-variance) counts, so
    // the event manager confirms which recount-worklist products she counted here.
    // Routed by kind, saved per-evkey to its own state file the recount dashboard
    // reads. The manager needs NO dashboard access — this is a public form.
    if (report.kind === "recount_count") {
      const RC = "docs/state/recount_manager_counts.json";
      const rcKey = String(report.evkey || "unknown");
      const toNums = (a) => Array.isArray(a) ? a.map(Number).filter((n) => !isNaN(n)) : [];
      const rec = {
        counted_pids: toNums(report.counted_pids),
        not_counted_pids: toNums(report.not_counted_pids),
        notes: String(report.notes || "").slice(0, 1200),
        manager: String(report.manager_name || report.manager || "").slice(0, 120),
        submitted_at: report.submitted_at || new Date().toISOString(),
        source: String(report.source || "recount-count-form").slice(0, 60)
      };
      for (let attempt = 0; attempt < 4; attempt++) {
        const cur = await ghGet(TOKEN, RC);
        const data = cur.json || { _updated_at: null, events: {} };
        if (!data.events) data.events = {};
        const prev = data.events[rcKey] || {};
        const subs = Array.isArray(prev.submissions) ? prev.submissions : [];
        subs.push(rec);
        data.events[rcKey] = {
          counted_pids: rec.counted_pids,
          not_counted_pids: rec.not_counted_pids,
          notes: rec.notes,
          manager: rec.manager,
          submitted_at: rec.submitted_at,
          submissions: subs.slice(-25)          // keep a short history
        };
        data._updated_at = new Date().toISOString();
        const res = await ghPutJson(TOKEN, RC, data, cur.sha,
          `recount-count: ${rcKey} — ${rec.manager || "?"} (${rec.counted_pids.length} counted)`);
        if (res === true) return reply({ ok: true, kind: "recount_count", counted: rec.counted_pids.length }, 200, cors);
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

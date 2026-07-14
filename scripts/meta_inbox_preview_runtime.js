// === handled.json sync (requires lauren_gh_token_v1 in localStorage, shared with all dashboards) ===
const TOKEN_KEY = "lauren_gh_token_v1";
const OWNER = "laurenlev10";
const REPO = "lauren-agent-hub-data";
const HANDLED_PATH = "docs/meta/handled.json";

function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; } }
function b64encode(s) { return btoa(unescape(encodeURIComponent(s))); }
function b64decode(s) { return decodeURIComponent(escape(atob(s.replace(/\s/g, "")))); }

async function fetchHandled() {
  const headers = { "Accept": "application/vnd.github+json" };
  const t = getToken();
  if (t) headers["Authorization"] = "Bearer " + t;
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/contents/${HANDLED_PATH}?ref=main&_=${Date.now()}`;
  const res = await fetch(url, { headers, cache: "no-store" });
  if (res.status === 404) return { data: {}, sha: null };
  if (!res.ok) throw new Error("GET " + res.status);
  const j = await res.json();
  return { data: JSON.parse(b64decode(j.content) || "{}"), sha: j.sha };
}

async function putHandled(data, sha) {
  const t = getToken();
  if (!t) throw new Error("No token in localStorage. Add it via the housing or launch dashboard first.");
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/contents/${HANDLED_PATH}`;
  const body = {
    message: "@meta: mark item handled (browser)",
    content: b64encode(JSON.stringify(data, null, 2)),
    branch: "main"
  };
  if (sha) body.sha = sha;
  const res = await fetch(url, {
    method: "PUT",
    headers: {
      "Accept": "application/vnd.github+json",
      "Authorization": "Bearer " + t,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(body)
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error("PUT " + res.status + ": " + txt.slice(0,200));
  }
}

async function markHandledRemote(dedupKey) {
  const { data, sha } = await fetchHandled();
  data[dedupKey] = { handled: true, handledAt: new Date().toISOString() };
  await putHandled(data, sha);
}

function fadeAndRemove(row) {
  if (!row) return;
  row.classList.add("handled-fade");
  setTimeout(() => {
    row.remove();
    // After removal: update the count + hide whole block if empty
    const block = document.querySelector(".attention-block");
    if (block) {
      const remaining = block.querySelectorAll(".attention-row").length;
      if (window.__updatePendingCount) window.__updatePendingCount(remaining);
      if (remaining === 0) {
        block.style.transition = "opacity 0.3s";
        block.style.opacity = "0";
        setTimeout(() => block.remove(), 350);
      }
    }
  }, 450);
}

async function markDone(btn) {
  const key = btn.dataset.key;
  if (!key) return;
  const row = btn.closest(".attention-row, .item");
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    await markHandledRemote(key);
    btn.textContent = "✓ Saved";
    setTimeout(() => fadeAndRemove(row), 600);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "✓ Done";
    if (String(e).includes("No token")) {
      promptForToken();
    } else {
      alert("Couldn't save: " + e);
    }
  }
}

// Also mark handled when Lauren clicks 💬 Reply — she's going to Meta to respond.
function markReplyClicked(link) {
  const key = link.dataset.key;
  if (!key) return;
  markHandledRemote(key).catch(e => console.warn("dedup save failed:", e));
  const row = link.closest(".attention-row, .item");
  if (row) setTimeout(() => fadeAndRemove(row), 1500);
}

function promptForToken() {
  if (document.getElementById("token-banner-existing")) return;
  const banner = document.createElement("div");
  banner.id = "token-banner-existing";
  banner.className = "token-banner";
  const html = [
    "<strong>⚠️ Save needs your GitHub token (one-time setup).</strong><br>",
    "Paste your fine-grained PAT (write access to lauren-agent-hub-data) below — it stays in your browser only:<br>",
    "<input id=\"tok-in\" type=\"password\" placeholder=\"github_pat_…\">",
    "<button id=\"tok-save\">Save token</button>"
  ].join("");
  banner.innerHTML = html;
  document.body.insertBefore(banner, document.body.firstChild);
  document.getElementById("tok-save").addEventListener("click", () => {
    const v = document.getElementById("tok-in").value.trim();
    if (!v) return;
    try { localStorage.setItem(TOKEN_KEY, v); } catch (e) {}
    banner.remove();
    alert("Token saved. Try again.");
  });
}

// On page load: hide already-handled items defensively
(async () => {
  try {
    const { data } = await fetchHandled();
    document.querySelectorAll(".attention-row[data-dedup-key]").forEach(row => {
      if (data[row.dataset.dedupKey] && data[row.dataset.dedupKey].handled) {
        fadeAndRemove(row);
      }
    });
  } catch (e) { /* server-side filter is the source of truth */ }
})();

// ===== Inline reply sender (added 2026-05-08) =====
// Sends repository_dispatch to GitHub which triggers meta-send-reply.yml workflow.
// Workflow then calls Meta API to send the message and updates handled.json.

async function sendReply(btn) {
  const key  = btn.dataset.key;
  const tid  = btn.dataset.tid;
  const kind = btn.dataset.kind;  // "messenger" | "fb_comment" | "ig_comment"
  const row  = btn.closest(".attention-row");
  const ta   = row && row.querySelector("textarea.att-reply");
  if (!ta) return;
  const text = (ta.value || "").trim();
  if (!text) {
    alert("נא להזין טקסט תשובה");
    return;
  }
  if (!tid || !kind) {
    alert("חסר זיהוי target_id או reply_kind — לא יכול לשלוח");
    return;
  }

  const token = (function() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; } })();
  if (!token) {
    promptForToken();
    return;
  }

  btn.disabled = true;
  btn.textContent = "📤 Sending…";

  try {
    const res = await fetch("https://api.github.com/repos/laurenlev10/lauren-agent-hub-data/dispatches", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        event_type: "send-meta-reply",
        client_payload: {
          dedup_key: key,
          target_id: tid,
          reply_kind: kind,
          reply_text: text
        }
      })
    });

    if (!res.ok) {
      const errText = await res.text();
      throw new Error("HTTP " + res.status + ": " + errText.slice(0, 200));
    }

    btn.textContent = "✓ Sent! (workflow running)";
    btn.classList.add("sent");

    // Mark as handled locally + remotely
    try { await markHandledRemote(key); } catch(e) { console.warn("handled save failed:", e); }
    setTimeout(() => fadeAndRemove(row), 1500);

  } catch (e) {
    btn.disabled = false;
    btn.textContent = "📤 Send Reply";
    alert("שליחה נכשלה: " + e);
  }
}

window.sendReply = sendReply;

// ===== Comment moderation: 🙈 Hide / 🗑️ Delete / 🚫 Block (added 2026-07-14) =====
// Buttons injected into each COMMENT row fire a repository_dispatch
// ("moderate-meta-comment") → meta-moderate.yml, which calls the Meta Graph API
// with the server-side Page token and writes a per-item result to
// docs/meta/moderation_results.json. We poll that file so the real outcome shows
// fast (success OR the actual failure reason). The comment id + platform are
// parsed from the dedup key so this works even for IG rows with empty target_id.
//   hide   → reversible, quiet; KEEPS the row so you can then Block/Delete
//   delete → permanent (FB + IG); removes the row
//   block  → FB: page-ban via API (best-effort); IG: open profile to block manually

const MOD_RESULTS_PATH = "docs/meta/moderation_results.json";

function _parseCommentRef(dedupKey) {
  const m = /^(fb|ig)-comment:(.+)$/.exec(dedupKey || "");
  if (!m) return null;
  return { platform: m[1], id: m[2] };
}

async function fetchModerationResults() {
  const headers = { "Accept": "application/vnd.github+json" };
  const t = getToken();
  if (t) headers["Authorization"] = "Bearer " + t;
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/contents/${MOD_RESULTS_PATH}?ref=main&_=${Date.now()}`;
  const res = await fetch(url, { headers, cache: "no-store" });
  if (res.status === 404) return {};
  if (!res.ok) throw new Error("GET " + res.status);
  const j = await res.json();
  return JSON.parse(b64decode(j.content) || "{}");
}

// Poll the results file until the workflow records THIS action for THIS item
// (fresh: at >= when we dispatched). Returns {ok, reason} or null on timeout.
async function waitForModerationResult(dedupKey, action, startedAt, opts) {
  const attempts = (opts && opts.attempts) || 30;
  const intervalMs = (opts && opts.intervalMs) || 4000;
  for (let i = 0; i < attempts; i++) {
    await new Promise(function (r) { setTimeout(r, intervalMs); });
    try {
      const results = await fetchModerationResults();
      const rec = results && results[dedupKey];
      if (rec && rec.action === action && rec.at) {
        const at = Date.parse(rec.at);
        if (!isNaN(at) && at >= (startedAt - 4000)) {
          return { ok: !!rec.ok, reason: rec.reason || "" };
        }
      }
    } catch (e) { /* transient network — keep polling */ }
  }
  return null;
}

async function dispatchModerate(action, dedupKey, commentId, platform, btn, row) {
  const token = getToken();
  if (!token) { promptForToken(); return; }
  const startedAt = Date.now();
  const workingTxt = { hide: "🙈 מסתיר…", delete: "🗑️ מוחק…", block: "🚫 חוסם…" };
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = workingTxt[action] || "…";

  // 1) Fire the dispatch. A 200 here only means GitHub accepted the request —
  //    not that Meta succeeded — so we do not remove the row yet.
  try {
    const res = await fetch("https://api.github.com/repos/laurenlev10/lauren-agent-hub-data/dispatches", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        event_type: "moderate-meta-comment",
        client_payload: { dedup_key: dedupKey, comment_id: commentId, platform: platform, action: action }
      })
    });
    if (!res.ok) { const t = await res.text(); throw new Error("HTTP " + res.status + ": " + t.slice(0, 160)); }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = orig;
    alert("לא הצלחתי לשלוח את הבקשה: " + e);
    return;
  }

  // 2) Wait for the server's real result.
  btn.textContent = "⏳ מאשר מול Meta…";
  const result = await waitForModerationResult(dedupKey, action, startedAt);

  if (result && result.ok) {
    const doneTxt = { hide: "✓ הוסתר", delete: "✓ נמחק", block: "✓ נחסם" };
    btn.textContent = doneTxt[action] || "✓";
    btn.classList.add("sent");
    btn.disabled = true;
    if (action === "hide") {
      // Hide is quiet + reversible — keep the row so Lauren can still Block/Delete
      // the same comment. Already marked handled on the server, so it won't return.
      if (row) row.classList.add("mod-hidden-kept");
    } else {
      setTimeout(() => fadeAndRemove(row), 1000);   // delete/block are terminal
    }
  } else if (result && !result.ok) {
    // Real failure — show the actual reason and KEEP the row on the board.
    btn.disabled = false;
    btn.textContent = "⚠ לא הצליח";
    btn.classList.add("mod-failed");
    alert(result.reason || "הפעולה נכשלה מול Meta — התגובה נשארת בדשבורד.");
  } else {
    // No result recorded in time.
    btn.disabled = false;
    btn.textContent = "⚠ אין אישור";
    btn.classList.add("mod-failed");
    alert("לא קיבלתי אישור מ-Meta בזמן. התגובה נשארת בדשבורד — נסי שוב עוד רגע, או 'פתחי במטה'.");
  }
}

function onModerateClick(btn) {
  const action    = btn.dataset.action;
  const dedupKey  = btn.dataset.key;
  const commentId = btn.dataset.cid;
  const platform  = btn.dataset.platform;
  const row = btn.closest(".attention-row");

  // BLOCK is MANUAL for both platforms: Meta does not expose the commenter's
  // identity to the API (that's why the author shows "?"), so a programmatic ban
  // is impossible. Open the profile (IG) or the post (FB) so Lauren can block
  // where Meta DOES show the name, in one place. No API dispatch.
  if (action === "block") {
    let url = "";
    if (platform === "ig") {
      let handle = "";
      const whoEl = row && row.querySelector(".who");
      if (whoEl) handle = (whoEl.textContent || "").trim().replace(/^@/, "");
      if (handle) url = "https://instagram.com/" + encodeURIComponent(handle);
    }
    if (!url) {
      const link = row && row.querySelector("a.reply-btn-secondary");
      url = (link && link.getAttribute("href")) || "https://www.facebook.com/";
    }
    window.open(url, "_blank", "noopener");
    const msg = (platform === "ig")
      ? "אינסטגרם לא מאפשרת חסימה דרך ה-API — פתחתי את הפרופיל של המגיב.\nלחצי ⋯ ← Block, ואז 'סמני כטופל'."
      : "פייסבוק לא חושפת את זהות המגיב ל-API (לכן מופיע '?'), אז אי אפשר לחסום אוטומטית.\nפתחתי את הפוסט — מצאי את התגובה ← ⋯ ← Ban / Hide, ואז 'סמני כטופל'.";
    alert(msg);
    return;
  }

  const confirmMsg = {
    delete: "למחוק את התגובה לצמיתות? זו פעולה בלתי הפיכה (התגובה נעלמת לכולם).",
    block:  "לחסום את המגיב מהעמוד? הוא לא יוכל להגיב יותר."
  };
  if (confirmMsg[action] && !confirm(confirmMsg[action])) return;

  dispatchModerate(action, dedupKey, commentId, platform, btn, row);
}
window.onModerateClick = onModerateClick;

// Inject the moderation button group into every comment row on load.
(function injectModerationButtons() {
  function build() {
    document.querySelectorAll('.attention-row').forEach(function (row) {
      const kind = row.dataset.replyKind || "";
      if (kind !== "fb_comment" && kind !== "ig_comment") return;   // comments only
      if (row.querySelector(".mod-actions")) return;                 // already added
      const ref = _parseCommentRef(row.dataset.dedupKey);
      if (!ref) return;
      const actions = row.querySelector(".att-actions");
      if (!actions) return;
      const wrap = document.createElement("div");
      wrap.className = "mod-actions";
      const TITLES = {
        hide: "מסתיר את התגובה מהציבור (הפיך — המגיב עדיין רואה את עצמו)",
        delete: "מוחק את התגובה לצמיתות (לכולם)",
        block: "פותח את הפוסט/פרופיל לחסימה ידנית (Meta לא מאפשרת חסימה דרך API)"
      };
      const mk = function (action, label, cls) {
        const b = document.createElement("button");
        b.className = "mod-btn " + cls;
        b.textContent = label;
        b.title = TITLES[action] || "";
        b.dataset.action = action;
        b.dataset.key = row.dataset.dedupKey;
        b.dataset.cid = ref.id;
        b.dataset.platform = ref.platform;
        b.setAttribute("onclick", "onModerateClick(this)");
        return b;
      };
      wrap.appendChild(mk("hide",   "🙈 הסתר", "mod-hide"));
      wrap.appendChild(mk("delete", "🗑️ מחק",  "mod-del"));
      wrap.appendChild(mk("block",  "🚫 חסום", "mod-block"));
      actions.appendChild(wrap);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();

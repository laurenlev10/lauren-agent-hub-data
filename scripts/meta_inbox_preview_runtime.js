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
// Buttons are injected into each COMMENT row (fb_comment / ig_comment) and fire a
// repository_dispatch (event_type "moderate-meta-comment") → meta-moderate.yml,
// which calls the Meta Graph API with the server-side Page token.
//   hide   → reversible, quiet (comment stays visible only to its author)
//   delete → permanent, both FB + IG
//   block  → FB: page-ban via API (best-effort); IG: open profile to block manually
// The comment id + platform are parsed from the dedup key (fb-comment:<id> / ig-comment:<id>)
// so this works even for IG rows whose data-target-id is empty.

function _parseCommentRef(dedupKey) {
  // "fb-comment:123_456" → {platform:"fb", id:"123_456"}
  const m = /^(fb|ig)-comment:(.+)$/.exec(dedupKey || "");
  if (!m) return null;
  return { platform: m[1], id: m[2] };
}

async function dispatchModerate(action, dedupKey, commentId, platform, btn, row) {
  const token = getToken();
  if (!token) { promptForToken(); return; }
  const workingTxt = { hide: "\ud83d\ude48 \u05de\u05e1\u05ea\u05d9\u05e8\u2026", delete: "\ud83d\uddd1\ufe0f \u05de\u05d5\u05d7\u05e7\u2026", block: "\ud83d\udeab \u05d7\u05d5\u05e1\u05dd\u2026" };
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = workingTxt[action] || "\u2026";

  // 1) Fire the dispatch. A 200 here only means GitHub ACCEPTED the request —
  //    NOT that Meta succeeded. So we do not remove the row yet.
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
    alert("\u05dc\u05d0 \u05d4\u05e6\u05dc\u05d7\u05ea\u05d9 \u05dc\u05e9\u05dc\u05d5\u05d7 \u05d0\u05ea \u05d4\u05d1\u05e7\u05e9\u05d4: " + e);
    return;
  }

  // 2) Wait for the SERVER to confirm the action really worked. The workflow
  //    writes handled.json ONLY when Meta returns success (hide/delete/block),
  //    so its appearance there is our success signal. If it never appears
  //    (Meta failed, block not possible, etc.) we KEEP the row on the board.
  btn.textContent = "\u23f3 \u05de\u05d0\u05e9\u05e8 \u05de\u05d5\u05dc Meta\u2026";
  const ok = await waitForModerationConfirm(dedupKey, action);
  if (ok) {
    const doneTxt = { hide: "\u2713 \u05d4\u05d5\u05e1\u05ea\u05e8", delete: "\u2713 \u05e0\u05de\u05d7\u05e7", block: "\u2713 \u05e0\u05d7\u05e1\u05dd" };
    btn.textContent = doneTxt[action] || "\u2713";
    btn.classList.add("sent");
    btn.disabled = true;
    if (action === "hide") {
      // Hide is quiet + reversible \u2014 KEEP the row so Lauren can still escalate
      // to Block/Delete on the same comment (hide first, then block the person).
      // It is already marked handled on the server, so it will not come back on
      // the next scan; only Delete/Block (terminal) fade the row away.
      if (row) row.classList.add("mod-hidden-kept");
    } else {
      setTimeout(() => fadeAndRemove(row), 1000);
    }
  } else {
    btn.disabled = false;
    btn.textContent = "\u26a0 \u05dc\u05d0 \u05d4\u05e6\u05dc\u05d9\u05d7 \u2014 \u05e0\u05e1\u05d9 \u05e9\u05d5\u05d1";
    btn.classList.add("mod-failed");
    alert("\u05d4\u05e4\u05e2\u05d5\u05dc\u05d4 \u05dc\u05d0 \u05d0\u05d5\u05e9\u05e8\u05d4 \u05de\u05d5\u05dc Meta \u2014 \u05d4\u05ea\u05d2\u05d5\u05d1\u05d4 \u05e0\u05e9\u05d0\u05e8\u05ea \u05d1\u05d3\u05e9\u05d1\u05d5\u05e8\u05d3.\n(\u05d0\u05d5\u05dc\u05d9 \u05d7\u05e1\u05d9\u05de\u05d4 \u05e9\u05dc\u05d0 \u05e0\u05d9\u05ea\u05df \u05dc\u05d1\u05e6\u05e2, \u05d0\u05d5 \u05e9\u05d2\u05d9\u05d0\u05d4 \u05d6\u05de\u05e0\u05d9\u05ea). \u05e0\u05e1\u05d9 \u05e9\u05d5\u05d1 \u05d0\u05d5 \u05d4\u05e9\u05ea\u05de\u05e9\u05d9 \u05d1'\u05e4\u05ea\u05d7\u05d9 \u05d1\u05de\u05d8\u05d4'.");
  }
}

// Poll handled.json (via the GitHub Contents API — reflects commits in real time)
// until the moderate workflow marks THIS item handled, or we time out (~2 min).
async function waitForModerationConfirm(dedupKey, action, opts) {
  const attempts = (opts && opts.attempts) || 24;
  const intervalMs = (opts && opts.intervalMs) || 5000;
  const wantVia = "moderate-" + action;   // exact action, so hide\u2192block waits for the block
  for (let i = 0; i < attempts; i++) {
    await new Promise(function (r) { setTimeout(r, intervalMs); });
    try {
      const h = await fetchHandled();
      const rec = h && h.data && h.data[dedupKey];
      if (rec && rec.handled && rec.via === wantVia) {
        return true;
      }
    } catch (e) { /* transient network — keep polling */ }
  }
  return false;
}

function onModerateClick(btn) {
  const action   = btn.dataset.action;
  const dedupKey = btn.dataset.key;
  const commentId = btn.dataset.cid;
  const platform = btn.dataset.platform;
  const row = btn.closest(".attention-row");

  // Instagram BLOCK has no API — open the profile so Lauren blocks manually.
  if (action === "block" && platform === "ig") {
    let handle = "";
    const whoEl = row && row.querySelector(".who");
    if (whoEl) handle = (whoEl.textContent || "").trim().replace(/^@/, "");
    const url = handle ? ("https://instagram.com/" + encodeURIComponent(handle))
                       : ((row && row.dataset.metaUrl) || "https://instagram.com/");
    window.open(url, "_blank", "noopener");
    alert("אינסטגרם לא מאפשרת חסימה דרך המערכת — פתחתי לך את הפרופיל של המגיב.\nלחצי על ⋯ → Block כדי לחסום, ואז 'סמני כטופל'.");
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
      if (kind !== "fb_comment" && kind !== "ig_comment") return;      // comments only
      if (row.querySelector(".mod-actions")) return;                    // already added
      const ref = _parseCommentRef(row.dataset.dedupKey);
      if (!ref) return;
      const actions = row.querySelector(".att-actions");
      if (!actions) return;

      const wrap = document.createElement("div");
      wrap.className = "mod-actions";
      const mk = function (action, label, cls) {
        const b = document.createElement("button");
        b.className = "mod-btn " + cls;
        b.textContent = label;
        b.dataset.action = action;
        b.dataset.key = row.dataset.dedupKey;
        b.dataset.cid = ref.id;
        b.dataset.platform = ref.platform;
        b.setAttribute("onclick", "onModerateClick(this)");
        return b;
      };
      wrap.appendChild(mk("hide",   "🙈 הסתר",  "mod-hide"));
      wrap.appendChild(mk("delete", "🗑️ מחק",  "mod-del"));
      wrap.appendChild(mk("block",  "🚫 חסום",  "mod-block"));
      actions.appendChild(wrap);
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();

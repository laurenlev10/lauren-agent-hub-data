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
      const h2 = block.querySelector("h2");
      if (h2) h2.innerHTML = "👀 " + remaining + " items need your attention";
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

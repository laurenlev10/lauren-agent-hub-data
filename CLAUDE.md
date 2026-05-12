# CLAUDE.md — Lauren's Cowork session memory

**Read this BEFORE editing any HTML file in `Scheduled/outputs/`. Always.**

This file exists because Lauren got tired of Claude editing local copies of her dashboards that don't actually do anything. Every Cowork session must respect the rules below.

---

## 🛑 IRON RULE #1 — Dashboards live on GitHub, NEVER edit locally

Lauren's live dashboards are served from GitHub Pages: **https://laurenlev10.github.io/lauren-agent-hub-data/**

The local files in `Scheduled/outputs/*.html` are a **scratch directory**. They are on OneDrive, which silently truncates large files during sync (incidents on 2026-05-04 and 2026-05-08 — local files were 30–75% smaller than the deployed version). Editing them does **nothing** for Lauren — she sees the GitHub-served version on every device.

**When Lauren shows a screenshot of a dashboard and asks for a change, the change goes to the GitHub repo. Period. Don't ask which file — clone, edit, push.**

### Local file → deployed path mapping

| Local scratch (DO NOT EDIT)                          | Deployed file in repo                      | Public URL                                                     |
|-------------------------------------------------------|--------------------------------------------|-----------------------------------------------------------------|
| `Scheduled/outputs/launch_dashboard.html`             | `docs/launch/index.html`                   | https://laurenlev10.github.io/lauren-agent-hub-data/launch/    |
| `Scheduled/outputs/agent_hub.html`                    | `docs/index.html`                          | https://laurenlev10.github.io/lauren-agent-hub-data/           |
| `Scheduled/outputs/housing_dashboard.html`            | `docs/housing/index.html`                  | .../housing/                                                    |
| `Scheduled/outputs/inbox_dashboard.html`              | `docs/meta/index.html`                     | .../meta/                                                       |
| `Scheduled/outputs/mbs_dashboard.html`                | `docs/mbs/index.html`                      | .../mbs/                                                        |

Other dashboards under `docs/<subfolder>/` follow the same pattern. If unsure, `ls docs/` after clone.

### The edit flow (memorize this)

```bash
# 0. PAT is at this canonical path. It exists, it's 94 bytes, it's preauthorized.
PAT_FILE=/sessions/<session>/mnt/Claude/.claude/secrets/github_pat.txt
PAT=$(cat "$PAT_FILE")

# 1. Fresh clone (NEVER reuse a stale clone)
cd /tmp && rm -rf lauren-agent-hub-data
git clone "https://x-access-token:${PAT}@github.com/laurenlev10/lauren-agent-hub-data.git"
cd /tmp/lauren-agent-hub-data
git config core.hooksPath .githooks    # IRON RULE — activate guardrails
git config user.email "info@makeupblowoutsalegroup.com"
git config user.name "Lauren (via Cowork)"

# 2. Edit docs/<page>/index.html surgically — use str.replace / sed / python rewrite.
#    NEVER copy outputs/<page>.html over docs/<page>/index.html. Local is truncated.

# 3. Run the guardrail check — must pass closing tags + script balance + node --check + size sane
bash .githooks/check.sh docs/<page>/index.html

# 4. Commit + push
git add docs/<page>/index.html
git commit -m "<page>: <what changed and why>"
git push origin main
# Live within ~30–60 seconds.
```

### Triggers that mean "you're about to break IRON RULE #1"

If any of these are true, STOP and clone the repo instead:
- I'm about to call `Edit` on a path containing `Scheduled/outputs/` and the file ends in `.html`.
- I'm about to `cp Scheduled/outputs/*.html` into a repo `docs/` directory.
- The user shared a screenshot whose URL bar shows `laurenlev10.github.io` (or any of Lauren's dashboards) and I'm reaching for a local file.
- The user said "I don't see the change yet" after I edited a local HTML.

---

## 🛑 IRON RULE #2 — `state/memory.md` is the durable Lauren-memory file

Path: `Scheduled/NEW/agent-infra/sms-chat/state/memory.md` (964+ lines).

It contains every preference, every IRON RULE, every change-log entry across her agents. **Read it whenever the user asks about a preference or workflow** — almost every "how does Lauren want X done" answer is in there. Don't paraphrase from this file unless I've actually opened it in the current session.

---

## ⭐ Business context — Instagram Reel shares are the #1 metric (set 2026-05-10 PM)

Lauren's directive (verbatim): "העסק שלנו מאוד תלוי בכל מה שקשור לשיתוף הריל הזה כי יש קריאה לפעולה בכל הדפי נחיתה ובקמפיינים שלנו לשתף את הריל על מנת לקבל מתנה באירוע - המטרה לקבל כמה שיותר שיתופים אורגניים בנוסף למה שאנחנו מוציעים על הפירסום".

**The mechanic:** every paid landing page (English / Spanish / TikTok) AND every paid Meta/TikTok campaign carries a CTA: "share this Reel to get a gift at the event". So attendees who came via paid acquisition convert into ORGANIC amplifiers — each share they make becomes free reach for the next attendee. **Shares are the lever that converts paid spend into compounding organic reach.**

**What this means for any agent / workflow / dashboard touching Reels:**

1. **`shares` is the headline metric.** Plays / reach / likes are context; shares are the conversion. Surface shares first, biggest, most prominent. (INSTA REEL modal already does this in `docs/launch/index.html`.)
2. **Per-event Reel link is per-event.** Each event has its own dedicated Reel — Lauren creates a fresh one for each weekend. She'll paste the URL manually via the 🔗 chip on the launch dashboard whenever a new Reel is ready. The `insta-reel-share-scan` workflow's auto-detect (most-recent pinned reel) is the fallback when she hasn't pasted yet.
3. **Trends > absolutes.** A 50-share event that grew +15 shares/hour during the live window is doing better than a 200-share event flat over 3 days. Future Reel analytics should weight rate-of-change heavily — the INSTA REEL modal already shows "ממוצע עליית שיתופים לשעה" prominently for this reason.
4. **Staff performance proxy.** A flat share count during the live event window suggests staff aren't reminding attendees to share. Workflows that ping Lauren about under-performing events should compare share-rate against per-event baselines (currently we just show absolute numbers; this is a future enhancement).
5. **Never collapse the share metric.** When summarizing analytics for Lauren (SMS digests, dashboard pills, weekly summaries), shares MUST appear as their own field — not folded into a generic "engagement" or "interactions" bucket. Lauren reads share counts directly and uses them for decisions about staffing, campaign budget, and event tiering.

**When in doubt:** ask "what does this do to the share-rate signal Lauren is tracking?" If it dilutes, obscures, or de-prioritizes shares, it's the wrong design.

---

## Useful paths (fast lookup)

| Thing                           | Path                                                                        |
|---------------------------------|-----------------------------------------------------------------------------|
| Lauren memory                   | `Scheduled/NEW/agent-infra/sms-chat/state/memory.md`                        |
| Deploy mechanics (full)         | `Scheduled/NEW/deploy-dashboard/SKILL.md`                                   |
| Architect (cross-agent rules)   | `Scheduled/NEW/agent-architect/SKILL.md`                                    |
| GitHub PAT (canonical)          | `.claude/secrets/github_pat.txt` (fine-grained — push only, can't create repos) |
| GitHub PAT (broad, for new repos) | `.claude/secrets/github_pat_stats_v1.txt` (classic, scopes `repo, workflow`) |
| Other secrets / API tokens      | `.claude/secrets/` (eventbrite, simpletexting, ga4, qb, meta, tiktok, ...)  |
| The repo (live source of truth) | `laurenlev10/lauren-agent-hub-data` on `main`, served from `docs/`          |
| Public events site repo         | `laurenlev10/themakeupblowout-events` → `events.themakeupblowout.com`       |
| Public QR-subscribe site repo   | `laurenlev10/themakeupblowoutsale-group-site` → `www.themakeupblowoutsale-group.com` (replaces ClickFunnels, see 2026-05-11 PM in memory.md) |
| Per-event public stats page     | `themakeupblowout-events/docs/_template/stats.html.tpl` → `events.themakeupblowout.com/events/<slug>/stats.html` (Registrations + Paid Acquisition Meta+TikTok + pixel funnel, 2026-05-12) |
| Per-event signup snapshot       | `themakeupblowout-events/docs/state/registration_stats.json` — pushed by `registrations-6h.yml` every 6h, keyed by events-repo slug |
| Per-event growth time-series    | `themakeupblowout-events/docs/state/event_timeseries.json` — pushed by `marketing-stats.yml` every 6h, drives `/stats.html` chart |

---

## launch_dashboard.html — feature & data-shape cheat sheet

The launch dashboard at `docs/launch/index.html` is the most-edited file in the repo. Two key data structures to know about before touching it:

### `MANUAL_TASKS` (synced via `docs/launch/notes.json`)

Per-event manual fields. Key = `${city-slug}-${start_date}` (city slug only, no state suffix; e.g. `columbia-2026-05-08`).

| Field                       | Owner          | Purpose                                                                 |
|-----------------------------|----------------|-------------------------------------------------------------------------|
| `team_override`             | Lauren (UI)    | Override of `STAFF_DEFAULTS[evkey].team` from the xlsx                  |
| `logistics_override`        | Lauren (UI)    | Override of `STAFF_DEFAULTS[evkey].logistics` from the xlsx             |
| `image_override_url`        | Lauren (UI)    | Manual Canva link for event images (skips the agent)                    |
| `hall_photo`                | Lauren (UI)    | `{path, ext, size, name, uploaded_at}` for the per-event venue photo. Binary in `docs/launch/hall-photos/${evkey}.${ext}` |
| `insta_reel_url`            | UI / scanner   | Current Reel permalink — manual paste OR auto-detected pinned reel       |
| `insta_reel_url_set_by`     | UI / scanner   | `"manual"` \| `"auto"`                                                  |
| `insta_reel_url_set_at`     | UI / scanner   | ISO timestamp when set                                                  |
| `insta_reel_scans`          | scanner        | Append-only `[{scanned_at, event_local_hour, url_at_scan, media_id, shares, plays, reach, likes, comments, saved}]` (oldest first) |
| `tiktok_url`                | UI (🎵 TikTok 🔗 chip) | Per-event TikTok video permalink — manual paste only                  |
| `tiktok_url_set_by`         | UI             | `"manual"` \| `"backfill-..."`                                          |
| `tiktok_url_set_at`         | UI             | ISO timestamp when set                                                  |
| `fb_url`                    | UI (📘 FB Reel 🔗 chip — added 2026-05-11 PM) | Per-event Facebook Reel permalink — manual paste only |
| `fb_url_set_by`             | UI             | `"manual"` \| `"backfill-..."`                                          |
| `fb_url_set_at`             | UI             | ISO timestamp when set                                                  |
| `<task_id>` (boolean)       | Lauren (UI)    | Per-event manual checkboxes — see `MANUAL_TASK_DEFS` in launch HTML     |
| `updated_at`                | both           | ISO timestamp of last write                                             |

**Adding a new MANUAL_TASKS field?** Update this table, plus the schema section in `Scheduled/NEW/eventbrite-setup/dashboard.md`. Other agents that touch notes.json (`pr-organization`, `pr-influencer`, etc.) must NOT clobber unknown keys — always merge, never replace.

### Per-row UI rules

- **One render path** — `renderSchedule()` covers both `period=future` and `period=past`. Per IRON RULE 2026-05-05, any new per-row UI MUST render on past tab too (Lauren keeps history forever; preserves staff-performance evidence).
- **Date cell stack** — `dateCell` (range string), then `liveBadge` (rendered when today is in event's Fri-Sun window), then `hallPhotoHtml`. Past events keep `.col-date` and `.col-city` black via specificity override.
- **Status cell stack** — `statusBadge` (Next badge + signed/done/tentative pill), then `rowBtn` (the agent button row).
- **Agent button row** — order: 🎟️ Eventbrite ✓ → 📋 List ✓ → 📑 Contract → 🔍 Scout → 🎨 Images → 🔗 image chip → 🎬 Reel → 📸 INSTA REEL → 🔗 reel chip → 🎵 TikTok → 🔗 tt chip → 📘 FB Reel → 🔗 fb chip → 📲 Campaigns → 📊 Summary → 🤝 PR → ✨ PR Influencers → 🔮 Forecast → 📍 Landing.
- **🛑 Common bug:** when adding a new button block after an existing if/else, double-check the closing brace placement. INSTA REEL was first shipped inside the Canva-Reel `else` branch by mistake (commit `f521be7` fixed it). For independent concerns, always close the prior if/else BEFORE the new block.

### Persistent maps in the HTML body

These are JS `const` bl
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

These are JS `const` blocks at the top of the inline script. Agents upsert their slice via the deploy pipeline:

| Map                | Owner agent              | Schema                                                              |
|--------------------|--------------------------|---------------------------------------------------------------------|
| `SCHEDULE`         | xlsx upload (Lauren)     | All events by year — city, state, start_date, end_date, venue, address, status, tier |
| `STAFF_DEFAULTS`   | xlsx upload (Lauren)     | `{evkey: {team, logistics}}` — defaults from the bookings sheet     |
| `SETUPS`           | eventbrite-setup         | Eventbrite event URLs + SimpleTexting list IDs                      |
| `LIST_STATS`       | registrations-6h         | Subscriber counts per SMS list, daily delta (every 6h; also pushed slim to events-repo as `registration_stats.json` for /stats.html) |
| `EVENTBRITE_STATS` | registrations-6h         | Registrations / capacity / fill-rate / today_delta (every 6h; also pushed to events-repo) |
| `HISTORICAL_LISTS` | (frozen)                 | Year-over-year subscriber totals                                    |
| `CONTRACTS`        | contract-review          | Per-event contract review state                                     |
| `SCOUTS`           | mbs-city-scout           | Scout reports                                                       |
| `IMAGES`           | canva-event-images       | Canva folder + image counts                                         |
| `REELS`            | canva-reel               | Primary + refresh reel URLs                                         |
| `PR_ORGS`, `PR_INFLUENCERS` | pr-organization / pr-influencer | PR contact lists per event                                |
| `LANDING_PAGES`    | landing-page-builder     | Per-event landing page URLs                                         |
| `META_CAMPAIGNS`   | meta-campaigns           | Ad-set + creative URLs                                              |
| `FORECASTS`        | event-forecast           | Revenue forecast + confidence                                       |
| `SUMMARIES`        | mbs-event-summary        | Post-event P&L summary                                              |

---

## Per-event stats page — data shape (2026-05-12 PM)

Lives at `themakeupblowout-events/docs/_template/stats.html.tpl` → built into `docs/events/<slug>/stats.html`. Reads three JSON files from `themakeupblowout-events/docs/state/`:

- `registration_stats.json` — Eventbrite + SMS counts (always rendered, "📋 הרשמות לאירוע" section)
- `event_timeseries.json` — growth snapshots for the chart
- `event_analytics.json` — pixel + paid funnel data (cross-pushed from `lauren-agent-hub-data` by `marketing-stats.yml`)

The `event_analytics.json` per-event shape (`events.<slug>.*`) — written by `lauren_stats.py::aggregate_for_events`:

| Block               | Owner / source                       | What's inside                                                               |
|---------------------|--------------------------------------|-----------------------------------------------------------------------------|
| `views`             | GA4 (`fetch_ga4_event_data`)         | total, by_source, by_campaign, by_lang                                      |
| `conversions`       | GA4                                  | total, by_source                                                            |
| `ad_spend`          | Meta + TikTok APIs                   | meta, tiktok (rollup totals)                                                |
| `meta`              | Meta API (`fetch_meta_pixel_events`) | `{spend, revenue, impressions, clicks, landing_page_views, ctr, cpc, cpm, cost_per_lpv, top_ads[]}` — same shape as `tiktok` for symmetry |
| `tiktok`            | TikTok Marketing API (`fetch_tiktok_pixel_events`) | `{spend, impressions, clicks, landing_page_views, conversions, ctr, cpc, cpm, cost_per_lpv, top_ads[]}` — top_ads sorted by LPV desc (max 5) |
| `roas_by_source`    | derived                              | `{meta: 2.4, tiktok: 1.8}` etc                                              |
| `funnel`            | derived (`compute_funnel`)           | impressions → page_views → form_submits → sms_registered → eventbrite_registered |
| `rates`             | derived                              | ctr, form_conversion, sms_capture, overall                                  |
| `forecast`          | derived                              | current, daily_rate, projected_total, target, days_remaining, status, gap   |
| `anomalies`         | `detect_anomalies`                   | List of `{severity, metric, observed, expected, hypothesis}`                |

**The new "🎯 Paid Acquisition" section** in `stats.html.tpl` reads `evPixel.meta` and `evPixel.tiktok` side-by-side. Each platform card shows spend / impressions / clicks / CTR / CPL / CPC + top 3 ads + a deep-link to that platform's Ads Manager. Status pill on each card: `live` (spend > 0), `no data yet` (zero, no token), `API pending` (TikTok only, while Marketing API approval is outstanding).

**🛑 Adding new TikTok-related campaigns:** Lauren's TikTok campaign names don't follow a slug convention — they're descriptive ("Traffic Best Post — Roseville, MN 2026 Leads", "Copy 1 of Roseville, MN 2026 Traffic | Best #1"). `_match_tiktok_slug` in `lauren_stats.py` matches by city+year inside the combined campaign/adgroup/ad name. **Don't rename campaigns to drop the city or year** — that breaks slug matching and TikTok data stops being attributed to the event. Tested against the patterns Lauren uses today; if a future naming scheme breaks matching, extend the helper rather than papering over with manual mappings.

**🛑 IRON RULE — TikTok API is rejected as of 2026-05-12.** Ticket submitted at `ads.tiktok.com/athena/requester/boards/...` (Marketing API → Access Token & Authorization). Until approved, `fetch_tiktok_pixel_events` returns `{}` and every event's `tiktok` block stays at zeros. The stats page surfaces this with a yellow "API pending" pill on the TikTok card. **Don't fake / stub the TikTok numbers in the meantime** — Lauren reads them and makes spend decisions; phantom data is worse than zeros + clear status. When the API is approved (tracked as task #2 in the agent task list), the next scheduled `marketing-stats.yml` run picks up the change automatically.

---

## 🛑 IRON RULE #3 — Every workflow MUST SMS Lauren on failure

Set 2026-05-10 PM: "תמיד תשלח לי הודעת טקסט עם משימה מתוזמנת לא עובדת כן? זה חוק ברזל".

Every `.github/workflows/*.yml` ends with this step (paste it at the end of the last job's `steps:`):

```yaml
      - name: SMS Lauren on failure
        if: failure()
        env:
          SIMPLETEXTING_TOKEN: ${{ secrets.SIMPLETEXTING_TOKEN }}
          LAUREN_PHONE:        ${{ secrets.LAUREN_PHONE || '4243547625' }}
          WORKFLOW_NAME:       ${{ github.workflow }}
          JOB_NAME:            ${{ github.job }}
          RUN_URL:             ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
        run: python3 scripts/notify_failure.py
```

`scripts/notify_failure.py` is the shared helper — sends SMS to Lauren only (Eli stays out of operational noise). Fail-soft: SMS errors are logged, never re-raised. When creating any new workflow, copy this block verbatim before opening the PR — no exceptions.

---

## GitHub Actions workflows (auto-runs, no Cowork session needed)

In `.github/workflows/` of `lauren-agent-hub-data`:

| Workflow                             | When it runs                              | What it does                                                          |
|--------------------------------------|--------------------------------------------|-----------------------------------------------------------------------|
| `daily-sms-list-counts.yml`          | Daily 8 AM PT                             | Refresh LIST_STATS map in launch/index.html                           |
| `weekly-eventbrite-counts.yml`       | Weekly                                    | Refresh EVENTBRITE_STATS map                                          |
| `marketing-stats.yml`                | (per its schedule)                        | GA4 + Meta Ads + TikTok + ST → marketing dashboard                    |
| `meta-inbox-daily.yml`               | Daily                                     | Pull IG/FB DMs + comments → inbox dashboard                           |
| `meta-send-reply.yml`                | On manual dispatch                        | Send reply to a Meta thread                                           |
| `refresh-meta-posts.yml`             | Every 6 hours                             | Refresh `docs/state/recent_meta_posts.json` for the reel picker       |
| `hall-photo-reminder.yml`            | Friday 9 AM PT                            | If today's event has no `hall_photo` set → SMS Lauren                 |
| `insta-reel-share-scan.yml`          | Hourly Fri/Sat/Sun                        | At event-local 12:00/14:00/17:00, fetch IG insights, append scan      |

**Required secrets** (set in repo Settings → Secrets, already configured): `META_PAGE_TOKEN`, `META_IG_BUSINESS_ID`, `META_FB_PAGE_ID`, `SIMPLETEXTING_TOKEN`, `LAUREN_PHONE`, `ELI_PHONE`, `EVENTBRITE_TOKEN`, `GA4_*`, `TIKTOK_*`.

### Scheduled tasks registry — `docs/scheduled-runs.json`

The "משימות אוטומטיות" widget on `agent_hub.html` (and the `docs/scheduled/` detail page) read `docs/scheduled-runs.json` to render live OK/LATE/OFF/— status per task. Convention for any new scheduled workflow:

1. Add an entry under `tasks[]` with: `id`, `label` (Hebrew), `frequency` (`hourly` / `daily` / `weekly`), `scheduleHuman`, `cron`, `enabled: true`, `lastRunAt: ""`, `agent`, `dashboard` (relative path under `docs/`).
2. Add a step in the workflow (before the IRON RULE #3 failure step) that bumps `tasks[id].lastRunAt` to now-UTC and commits the file. Mirror the pattern in `.github/workflows/marketing-stats.yml` or `insta-reel-share-scan.yml`.
3. Pick `frequency` carefully — widget LATE thresholds are 1.2h / 25h / 192h for hourly/daily/weekly. A workflow that runs only on weekends (e.g. `insta-reel-share-scan`) registers as `weekly` to avoid false LATE on weekdays.

---

## 🛑 IRON RULE #4 — Master checklist for any new recurring/scheduled task

Set 2026-05-10 PM. When Lauren says "every X hours/days/Friday/event-weekend, do Y" — meaning anything that is to repeat on a schedule — apply ALL of the following without asking. This consolidates rules accumulated across IRON RULE #1 (GitHub-source-of-truth), #2 (memory.md is durable), #3 (failure SMS), plus all the side-conventions below. Every box must be checked before the new workflow is considered done.

### A. The workflow file (`.github/workflows/<id>.yml`)

Steps in this exact order under the last (or only) job:

1. **`Checkout`** — `uses: actions/checkout@v4`
2. **The actual work** — Python or shell that produces the data/output
3. **Commit + push if changed** — guarded by `git diff --quiet`, with `git pull --rebase origin main` before `git push` to handle other workflows committing concurrently. Author: `lauren@noreply.github.com` / `Lauren (via Actions)`.
4. **Update `scheduled-runs.json` (refresh `lastRunAt`)** — bump only this task's id to current UTC ISO, set top-level `_updated_at`. Mirror exactly `.github/workflows/registrations-6h.yml` lines for this step.
5. **Commit `scheduled-runs.json` bump** — same author/email, separate commit so git history is clean ("scheduled-runs: bump <id> lastRunAt").
6. **`SMS Lauren on failure`** (`if: failure()`) — IRON RULE #3 verbatim block, ALWAYS the last step.

Top of file: include `permissions: contents: write` (needed for steps 3+5), and both `schedule:` cron + `workflow_dispatch: {}` (always allow manual run from the Actions tab — Lauren needs the safety valve).

### B. Registry entry in `docs/scheduled-runs.json`

```json
{
  "id":             "<kebab-case, matches workflow filename>",
  "label":          "<Hebrew, user-facing — emoji prefix optional>",
  "frequency":      "hourly | daily | weekly",
  "scheduleHuman":  "<human-readable e.g. 'Every 6 hours · :15 past'>",
  "cron":           "<UTC cron expression>",
  "enabled":        true,
  "lastRunAt":      "",
  "agent":          "<workflow id, same as `id`>",
  "dashboard":      "<relative path under docs/, e.g. launch/ or stats/>"
}
```

**Frequency selection (CRITICAL — false LATE alerts come from picking wrong):**

| Cron interval                          | Use frequency | Why                                                     |
|----------------------------------------|---------------|---------------------------------------------------------|
| Hourly or more often                   | `hourly`      | LATE threshold 1.2h covers hourly with margin           |
| 2h–24h (incl. every 6h, twice-daily)   | `daily`       | LATE threshold 25h tolerates the 6h gaps                |
| 1× per week, weekend-only, or rarer    | `weekly`      | LATE threshold 192h (8 days)                            |

Weekend-only or "only-during-event" workflows ALWAYS register as `weekly` even if the cron literal is `0 * * * 5,6,0` (because between events the gap reaches 5+ days).

### C. SMS conventions (recipient choice + format)

| Use case                              | Recipients         | Where                                                |
|---------------------------------------|--------------------|------------------------------------------------------|
| Operational alert (failure, LATE)     | Lauren only        | `notify_failure.py`, `scheduled_watchdog.py`         |
| Content per-run (scan results, summary) | Lauren + Eli     | inside the workflow's main script (loop both phones) |
| Personal reminder (e.g. upload photo) | Lauren only        | `hall-photo-reminder.yml`                            |
| Weekly digest                         | Lauren + Eli       | `weekly-eventbrite-counts.yml`                       |

Boilerplate for "Lauren + Eli, fail-soft per recipient":

```python
recipients = []
for env_key, label in [("LAUREN_PHONE", "Lauren"), ("ELI_PHONE", "Eli")]:
    v = os.environ.get(env_key, "").strip()
    if v: recipients.append((label, v))
for name, phone in recipients:
    try:
        send_sms(phone, body)
    except Exception as e:
        print(f"  SMS to {name} failed: {e}")
```

Body always Hebrew, concise, ends with a clickable URL (event page / dashboard / Actions log).

### D. Documentation pass

- `Scheduled/NEW/agent-infra/sms-chat/state/memory.md` — append a change-log entry with: what shipped, the commit hash, Lauren's verbatim directive (Hebrew quoted), and any lesson learned.
- This file (`CLAUDE.md`) — only update if the new task introduces a new schema field, a new map, or a new IRON RULE.
- `Scheduled/NEW/agent-architect/SKILL.md` — only update if the convention itself changed (rare).

### E. Pre-push verification

```bash
# YAML
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/<id>.yml'))"
# Python (if a script was added/edited)
python3 -c "import ast; ast.parse(open('scripts/<file>.py').read())"
# HTML (if an HTML file under docs/ was edited)
bash .githooks/check.sh docs/<page>/index.html
# Verify last step of last job is the failure-notify step:
python3 -c "import yaml; d=yaml.safe_load(open('.github/workflows/<id>.yml')); j=list(d['jobs'].values())[0]; assert j['steps'][-1].get('if')=='failure()', 'last step must be failure-notify'"
```

**🛑 API field names — copy verbatim from a working workflow.** When the new workflow calls an external API that another workflow already hits, GREP the existing workflow for the field names and copy them as-is. Do NOT paraphrase or "improve" them. The pre-push YAML/AST checks DO NOT catch wrong field names — they pass; then the runtime returns the default `0` and silently corrupts data. After committing, MANUALLY DISPATCH the new workflow once from the Actions tab BEFORE the first cron tick fires, and confirm the data lands correctly. 2026-05-10 incident: `registrations-6h` used `totalSubscribers` instead of `totalContactsCount` and zeroed every event's LIST_STATS on its first scheduled run; the dashboard showed `0 active · ↓ -568 today` across the board (commit `74cb18e` was the fix).

**🛑 Verify-after-write — every Python-based file edit MUST grep the new marker BEFORE `git add`.** A heredoc `python3 <<'PY' … PY` that builds `src = src.replace(...)` but FORGETS the closing `open(path, "w").write(src)` will silently produce a valid-looking run (assertions pass, no error, file size unchanged). The diff stage misses it because git compares disk-vs-HEAD, and the local edit never reached disk. 2026-05-10 incident: the event-local-time chip — 4 changes (CSS, STATE_TZ const, eventLocalTimeStr helper, render injection) were computed in memory, assertions passed, but `write(src)` was missing. Only a subsequent script's `setInterval` block landed. Result: a no-op deploy that looked correct in commit message but did nothing on the live page. Lauren had to flag it twice before I traced the root cause (commit `955a996` was the fix).

**Defensive process for any Python-script edit:**
1. End every heredoc with `open(path, "w").write(src); print("wrote N bytes")`.
2. Immediately after the heredoc, run `grep -c "<new marker>" <file>` and assert it's > 0.
3. If grep returns 0 even though assertions passed, the file write was missing — fix the script and re-run.
4. Only THEN `git add` + `git commit` + `git push`.

**Better path:** for surgical edits in tracked files I can already Read, prefer the `Edit` tool over Python heredocs. `Edit` writes atomically and errors loudly on miss; heredocs are silent on the most common mistake (forgotten `write`).

### F. Auto-coverage that comes for free

Any task registered in `docs/scheduled-runs.json` automatically gets:
- **LATE detection** within 4h via `scheduled-watchdog.yml` → SMS Lauren if it stops running.
- **Live status tile** on `agent_hub.html` → OK/LATE/OFF/— based on `lastRunAt`.
- **Failure SMS** within minutes if the run dispatched but threw → from the `if: failure()` step.

No extra wiring is needed for these — they activate the moment the registry entry exists and the workflow ends with the IRON RULE #3 step.

---

## 🛑 IRON RULE #5 — Per-event share URLs come from `notes.json`, NEVER from generic channel URLs

Set 2026-05-11 PM. Lauren's directive: "תמיד יחפשו את הלינקים לשיתוף האירוע שם ולא סתם ישימו לינק לחשבון סושייל הכללי. תעדכן בבקשה לעתיד."

When ANY agent generates content (landing page, SMS, email, ad creative, share button, etc.) for a specific event and needs to point users at a **shareable** post on IG / Facebook / TikTok, that URL **MUST** come from `lauren-agent-hub-data/docs/launch/notes.json` keyed by `<city-slug>-<start_date>`. The brand's generic channel URL (e.g. `instagram.com/themakeupblowoutsale/`) is a LAST-RESORT fallback, only valid when no event context exists.

Why this matters: every paid landing page + campaign carries a CTA to share THIS event's Reel for a free gift at the door. Sharing the wrong (generic) link kills the conversion mechanic — see "⭐ Business context" above.

The notes.json fields per-event (table also above):
- `insta_reel_url` — set by 📸 INSTA REEL chip OR `insta-reel-share-scan.yml` auto-detect
- `tiktok_url` — set by 🎵 TikTok 🔗 chip
- `fb_url` — set by 📘 FB Reel 🔗 chip (added 2026-05-11 PM)

Correct pattern (Python):
```python
note = notes.get(evkey, {})
ig_url = note.get("insta_reel_url") or DEFAULT_IG_CHANNEL    # ✓
fb_url = note.get("fb_url")         or DEFAULT_FB_CHANNEL    # ✓
tt_url = note.get("tiktok_url")     or DEFAULT_TT_CHANNEL    # ✓
```

When fixing this in an existing agent:
1. Locate every place the agent embeds an IG / FB / TikTok URL inside per-event content.
2. Replace hard-coded channel URLs with `notes.json` lookup + channel fallback.
3. If the agent reads from a derived JSON (like `themakeupblowout-events/docs/upcoming-events.json`), make sure the upstream that builds that JSON also pulls from `notes.json`. The `update_subscribe_target.py` workflow is the canonical example (does this for `subscribe_target.json` and `upcoming_events.json` since 2026-05-11 PM).
4. Add a comment in the SKILL.md pointing to this IRON RULE so future-Claude doesn't regress.

---

## 🛑 IRON RULE #6 — NO bit.ly (or any URL shortener) anywhere. Direct landing-page URLs only.

Set 2026-05-12. Lauren's directive: "אין יותר שימוש בלינקים של BITLY מעכשיו אלא רק הלינקים החדשים לדפי הנחיתה החדשים. יש לעדכן את זה בכל מקום שצריך ככלל ברזל."

**Background — the Cleveland Reel incident, 2026-05-12.** While creating an `@metaads` NEW Reel ad campaign for Cleveland, the agent attached the FB cross-post of the IG Reel as the ad creative (Option A, `object_story_id`). The IG caption contained `https://bit.ly/Cleveland-OH-2026`; Instagram auto-detected it as the post's CTA; the FB cross-post inherited it; Meta's Marketing API IGNORED the per-creative `call_to_action.link` override (existing-post creatives use the post's natural CTA — that override is silently dropped). So the ad's live CTA pointed at `bit.ly/Cleveland-OH-2026`, which 301-redirects to `themakeupblowoutsale-group.com/cleveland-oh-2026` — **404**. The whole bit.ly redirect chain belongs to the ClickFunnels era, and is broken everywhere events live now (`events.themakeupblowout.com`, since 2026-05-07).

**The rule (apply to every agent, today and going forward):**

1. **Ad copy / message body / captions** — no `bit.ly/*` URL (or any shortener: tinyurl, t.co, ow.ly, lnk.bio, etc.) inside `message`, `caption`, `description`, `title`, `link_description`, or any user-visible text. Agents that clone from a source containing bit.ly MUST strip it. The `text_substitute_real` regex in `meta-campaigns/api.py` already handles this — keep it.

2. **CTA / link fields** — `call_to_action.value.link`, `link_caption`, `object_story_spec.link`, and every analog in any other platform's API must point to `https://events.themakeupblowout.com/events/<slug>/[index-es.html]?utm_medium=paid&utm_campaign=<slug>` (paid) or the un-UTM'd variant (organic). Never a shortener.

3. **Existing-post mode (`object_story_id`)** — before attaching an existing IG/FB Reel as ad creative, the agent MUST fetch the post and assert `post.call_to_action.value.link` does NOT match `bit.ly` or any other shortener pattern. If it does, raise `BitlyInExistingPostCtaError` and SMS Lauren with the post permalink so she can either edit the caption or opt into Option B fresh-upload (`confirm_engagement_loss_acceptable=True`).

4. **Future Reel captions** — IG/FB Reels written by Lauren or any agent (`canva-reel`, manual posts, future automation) use the direct events URL, not a bit.ly. The "Phase 2: Bitly short link" line in `canva-reel/SKILL.md` is **permanently out of scope** — deleted, not deferred.

5. **SMS / PR / landing pages / dashboards** — same rule. Every URL Lauren or her agents put in front of a customer is the direct events URL.

**Defensive coding pattern** (paste into any new agent that emits a per-event URL):
```python
# IRON RULE #6 (2026-05-12) — NO bit.ly anywhere
SHORTENER_RE = re.compile(r"\b(bit\.ly|tinyurl\.com|t\.co|ow\.ly|lnk\.bio|cutt\.ly|rebrand\.ly)/", re.I)
assert not SHORTENER_RE.search(new_url or ""), f"shortener forbidden — use events.themakeupblowout.com direct URL: got {new_url!r}"
assert not SHORTENER_RE.search(ad_copy or ""), "shortener leaked into ad copy"
```

**Why no shortener (the longer-term argument):** a stale shortener fails silently. A direct URL fails loudly when the destination moves — you get a 404 at the canonical domain, the browser shows it, you fix it once. A shortener absorbs the failure: the redirect chain still resolves (HTTP 301 → 404), so monitoring tools see "the bit.ly works" and the 404 only shows up after someone clicks all the way through. Multiply by every channel (organic IG + FB + TikTok + paid Meta + SMS + email + PR drafts + landing-page share buttons + printed Eventbrite cards), and a single forgotten redirect rots Lauren's whole funnel quietly. For an events business where the window between "ad goes live" and "event over" is days, that's intolerable.

**Cleanup of existing artifacts (separate from the rule):** the bit.ly redirects themselves still exist (Lauren doesn't have a Bitly API token saved). Until Lauren provides one (`.claude/secrets/bitly_token.txt`), every NEW emission is direct-URL only, and any user-visible bit.ly is treated as a defect. Manual sweep of leftover IG/FB Reel captions for upcoming events is tracked outside the agent-rule change.

---

## Language

Default to **Hebrew** with Lauren — natural, conversational. Proper nouns / URLs stay in English.

---

_Last updated: 2026-05-12 PM — added the "Per-event stats page — data shape" section documenting the new Paid Acquisition (Meta + TikTok) block on `events.themakeupblowout.com/events/<slug>/stats.html`. Companion commits: `themakeupblowout-events` a10107e (template), `lauren-agent-hub-data` 004b5ed (richer TikTok fetcher + slug matching)._

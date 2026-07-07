#!/usr/bin/env python3
"""
tiktok_comment_watch.py — monthly check: has TikTok started supporting comment
retrieval for the advertiser's ads?

Background (2026-07-07): the unified @meta comment agent now also covers TikTok
ad comments (scripts/tiktok_inbox.py, same engine/rules). But the advertiser's
campaigns are **Smart+**, which TikTok's comment API does NOT support —
/comment/task/create/ rejects them ("does not support Upgraded Smart Plus ads")
or /comment/task/check/ returns FAILED. So no comments flow yet.

This watcher runs monthly, samples active ad groups through the async export,
and SMSes Lauren the moment ANY export COMPLETES (i.e. TikTok added Smart+
support, or a non-Smart+ campaign is live) — at which point the every-3h
tiktok-inbox workflow starts handling TikTok comments automatically.

Silent when nothing changed (still Smart+/FAILED). SMS only on the good signal.
"""
import sys, time, datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lauren_tiktok as TT
from lauren_sms import send_sms
import os

LAUREN_PHONE = os.environ.get("LAUREN_PHONE", "4243547625")


def main():
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    end = now.strftime("%Y-%m-%d 23:59:59")
    adv = TT.get_advertiser_id()
    if not adv or not TT.get_token():
        print("token/advertiser not set — skipping"); return

    ags = TT._enumerate_adgroup_ids(start[:10], end[:10])
    print(f"active ad groups: {len(ags)}")
    smartplus = failed = ok = 0
    ok_sample = None
    for ag in ags[:20]:
        try:
            created = TT._check(TT._tt_post("/comment/task/create/", {
                "advertiser_id": adv, "start_time": start, "end_time": end,
                "search_field": "ADGROUP_ID", "search_value": ag}), "create")
        except TT.TikTokPermissionError:
            print("  40001 — token lost the comment scope (needs re-auth)"); return
        except RuntimeError as e:
            if "Smart Plus" in str(e) or "40002" in str(e):
                smartplus += 1; continue
            print("  create err:", str(e)[:80]); continue
        tid = created.get("task_id")
        if not tid:
            continue
        status = None
        for _ in range(4):
            time.sleep(2)
            try:
                chk = TT._check(TT._tt_get("/comment/task/check/",
                      {"advertiser_id": adv, "task_id": tid}), "check")
            except Exception:
                break
            status = (chk.get("status") or "").upper()
            if status in ("COMPLETED", "SUCCESS", "FINISH", "DONE", "FAILED"):
                break
        if status == "FAILED" or status is None:
            failed += 1
        else:
            ok += 1
            ok_sample = ok_sample or (ag, status)

    print(f"smartplus_rejected={smartplus} export_failed={failed} export_ok={ok}")
    if ok:
        body = ("🎵 טיקטוק התחילו לתמוך בשליפת תגובות מודעות! "
                f"(export הצליח על {ok} קבוצות). המערכת האוטומטית (כל 3 שעות, "
                "אותם חוקים כמו אינסטגרם/פייסבוק) תתחיל לטפל בהן. "
                "כדאי לוודא שהפרסר קורא נכון את הפורמט בהרצה הראשונה. "
                "דשבורד: dashboard.themakeupblowout.com/meta/inbox-api-preview/")
        try:
            send_sms(LAUREN_PHONE, body)
            print("✓ SMS sent — TikTok comments now retrievable")
        except Exception as e:
            print("SMS failed:", str(e)[:120])
    else:
        print("still unsupported (Smart+/FAILED) — no SMS, will recheck next month")


if __name__ == "__main__":
    main()

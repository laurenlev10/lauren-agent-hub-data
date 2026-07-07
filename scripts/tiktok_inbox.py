#!/usr/bin/env python3
"""
tiktok_inbox.py — classify + (optionally) reply to TikTok AD comments, reusing
the exact same Claude engine as the Meta inbox (meta_inbox_preview.classify_smart).

Flow mirrors the Meta agent:
  1. fetch ad comments (lauren_tiktok.fetch_ad_comments) for the last N days
  2. skip ones already handled (handled.json, channel "tiktok-comment")
  3. classify each via classify_smart (guardrails -> Claude -> keyword fallback)
  4. Bucket A + --reply  -> post public reply via /comment/post/, mark handled
     Bucket B / NEG      -> merge into the SAME pending.json queue Lauren already
                            reviews (channel "TikTok comment")
     Bucket SKIP         -> auto-done (nothing)

Rules unchanged: replies English/Spanish only, one allowed link
(themakeupblowout.com/#events), never invent cities/dates, complaints/influencer
-> Lauren.

INERT until the Ad Comments scope is granted. Modes:
  --probe   fetch a tiny window, print raw payload + field names (verify parser)
  (default) classify + write pending, NO sends
  --reply   actually send Bucket A replies (live)
  --days N  lookback window (default 14)
"""
import argparse, json, os, sys, datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meta_inbox_preview as M
import lauren_tiktok as TT

REPO = Path(__file__).resolve().parent.parent
PEND = REPO / "docs/meta/inbox-api-preview/pending.json"
HANDLED = REPO / "docs/meta/handled.json"
CHANNEL = "tiktok-comment"


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="Dump the raw comment_list payload to verify field names once the scope is live.")
    ap.add_argument("--reply", action="store_true", help="LIVE: send Bucket A replies.")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(days=args.days)).strftime("%Y-%m-%d 00:00:00")
    end = now.strftime("%Y-%m-%d 23:59:59")

    if args.probe:
        adv = TT.get_advertiser_id()
        ags = TT._enumerate_adgroup_ids(start[:10], end[:10])
        print(f"active ad groups in window: {len(ags)}")
        if not ags:
            print("  (none — no comments to fetch)"); return
        for ag in ags[:8]:
            params = {"advertiser_id": adv, "start_time": start, "end_time": end,
                      "search_field": "ADGROUP_ID", "search_value": ag,
                      "sort_field": "CREATE_TIME", "sort_type": "DESC",
                      "page": 1, "page_size": 5}
            try:
                raw = TT._tt_get("/comment/list/", params)
            except Exception as e:
                print(f"  ag={ag[-6:]} PROBE ERROR: {e}"); continue
            code = raw.get("code")
            if code == 0:
                d = raw.get("data", {})
                rows = d.get("comments") or d.get("list") or []
                print(f"  ag={ag[-6:]} code=0 n={len(rows)} page_info={d.get('page_info')}")
                if rows:
                    print(json.dumps(rows[0], ensure_ascii=False, indent=2)[:2000])
                    return
            else:
                print(f"  ag={ag[-6:]} code={code} {raw.get('message','')[:70]}")
        return

    kb = M.load_kb(M._resolve_kb_path())
    kb["_venues"] = M.load_venues()
    kb["_post_context"] = None

    try:
        comments = TT.fetch_ad_comments(start_time=start, end_time=end)
    except TT.TikTokPermissionError as e:
        print(f"  ⏳ TikTok Ad Comments scope not granted yet — {e}")
        print("  (Re-run after the app scope is approved + advertiser re-authorized.)")
        return
    print(f"Fetched {len(comments)} TikTok ad comments ({args.days}d window)")

    handled = M.load_handled()
    pend = _load(PEND, {"_updated_at": None, "items": {}})
    items = pend.get("items", {})

    a = b = neg = skip = sent = 0
    for c in comments:
        cid = c.get("comment_id", "")
        if not cid:
            continue
        dkey = M.dedup_key(CHANNEL, cid)
        if handled.get(dkey, {}).get("handled"):
            continue
        kb["_seed"] = cid
        cls = M.classify_smart(c.get("text", ""), kb)
        bucket = cls.get("bucket")
        if bucket == "A":
            a += 1
            if args.reply:
                try:
                    TT.reply_to_comment(c, cls.get("reply") or "", dry_run=False)
                    handled[dkey] = {"handled": True, "handledAt": _now_iso(),
                                     "note": "tiktok auto-reply (Bucket A)"}
                    sent += 1
                except Exception as e:
                    print(f"  ⚠ reply failed {cid}: {str(e)[:100]}")
        elif bucket in ("B", "NEG"):
            b += (bucket == "B"); neg += (bucket == "NEG")
            items[dkey] = {
                "channel": "TikTok comment",
                "who": "@" + c.get("username", "?"),
                "what": c.get("text", "(no text)"),
                "url": c.get("reply_url", ""),
                "bucket": bucket,
                "dedup_key": dkey,
                "reply_kind": "tiktok_comment",
                "target_id": cid,
                "received": c.get("create_time", ""),
                "event_chip": cls.get("event_chip"),
                "draft": cls.get("reply") or "",
                "first_seen": items.get(dkey, {}).get("first_seen") or _now_iso(),
            }
        else:
            skip += 1

    pend["_updated_at"] = _now_iso()
    pend["items"] = items
    PEND.write_text(json.dumps(pend, ensure_ascii=False, indent=1), encoding="utf-8")
    HANDLED.write_text(json.dumps(handled, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Classified: A={a} (sent={sent}) B={b} NEG={neg} SKIP={skip}")


if __name__ == "__main__":
    main()

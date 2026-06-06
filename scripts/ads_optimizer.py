#!/usr/bin/env python3
"""
@ads-optimizer — recommendation engine (read-only brain).

Reads @stats (event_analytics.json: Meta + TikTok per-ad data + realized ROAS) and
emits PAUSE (weak ads wasting budget) + SCALE (winners worth more budget) recommendations
to docs/state/ads_optimizer.json.

SAFETY: this script NEVER touches Meta/TikTok. It only writes recommendations. Execution
happens elsewhere, and ONLY after Lauren approves each one in the dashboard (one-click).
Lauren's decisions (approved/dismissed/executed) are MERGE-preserved across runs per IRON RULE #7.

Thresholds (conservative, documented):
  MIN_SPEND      = 50    # need enough spend on an ad to judge it
  PAUSE_RATIO    = 1.5   # ad ≥50% WORSE than its channel benchmark → pause candidate
  SCALE_RATIO    = 0.7   # ad ≥30% BETTER than its channel benchmark → scale candidate
  SCALE_PCT      = 0.20  # suggested daily-budget bump for winners
  ACTIVE_DAYS    = 4     # only judge events whose Meta ads spent in the last N days (still live)
Metric per ad: CPL = spend/LPV when LPV>0 (Traffic), else CPC = spend/clicks (Leads).
Benchmark per (event, channel) = spend-weighted blended metric across that channel's ads.
"""
import json, os, sys, datetime, urllib.request, pathlib

EA_URL   = "https://events.themakeupblowout.com/state/event_analytics.json"
ROOT     = pathlib.Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "state" / "ads_optimizer.json"

MIN_SPEND, PAUSE_RATIO, SCALE_RATIO, SCALE_PCT, ACTIVE_DAYS = 50.0, 1.25, 0.85, 0.20, 4
SCALE_MIN_SPEND, ABS_PAUSE_MULT = 100.0, 1.2  # winners need real budget; pause needs CPL>1.2x cross-event mean too

def load_ea():
    # Prefer live events-site copy; fall back to any local copy.
    try:
        with urllib.request.urlopen(EA_URL, timeout=25) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ⚠ fetch EA failed ({e}); trying local")
        for p in [ROOT/"docs"/"state"/"event_analytics.json", pathlib.Path("/tmp/ea.json")]:
            if p.exists(): return json.loads(p.read_text())
        raise

def meta_active(ev, today):
    """Event still spending on Meta in the last ACTIVE_DAYS days?"""
    ts = (ev.get("meta") or {}).get("daily_timeseries") or []
    cut = today - datetime.timedelta(days=ACTIVE_DAYS)
    for r in ts:
        try:
            d = datetime.date.fromisoformat(r.get("date",""))
        except Exception:
            continue
        if d >= cut and (r.get("spend",0) or 0) > 0:
            return True
    return False

def ad_metric(a):
    """Return (metric_name, value-lower-is-better) for one ad. None if not judgeable."""
    spend = float(a.get("spend",0) or 0)
    lpv   = float(a.get("lpv",0) or a.get("landing_page_views",0) or 0)
    clk   = float(a.get("clicks",0) or 0)
    if spend < MIN_SPEND: return None
    if lpv > 0:  return ("CPL", spend/lpv)
    if clk > 0:  return ("CPC", spend/clk)
    return None

def blended(ads, metric_name):
    tot_s = sum(float(a.get("spend",0) or 0) for a in ads)
    if metric_name == "CPL":
        tot_d = sum(float(a.get("lpv",0) or a.get("landing_page_views",0) or 0) for a in ads)
    else:
        tot_d = sum(float(a.get("clicks",0) or 0) for a in ads)
    return (tot_s/tot_d) if tot_d > 0 else None

def build():
    ea = load_ea(); events = ea.get("events", {})
    mean_cpl = (ea.get("_averages") or {}).get("mean_cpl")  # cross-event absolute CPL guard
    today = datetime.date.today()
    recs = []
    benchmarks = {}
    for slug, ev in events.items():
        ms = float((ev.get("meta") or {}).get("spend",0) or 0)
        ts = float((ev.get("tiktok") or {}).get("spend",0) or 0)
        if ms + ts == 0: continue
        if not meta_active(ev, today):  # skip stale / finished campaigns
            continue
        realized = ev.get("realized") or {}
        for ch in ("meta","tiktok"):
            ads = list((ev.get(ch) or {}).get("top_ads") or [])
            judged = [(a, ad_metric(a)) for a in ads]
            judged = [(a,m) for a,m in judged if m]
            if len(judged) < 2:   # need ≥2 judgeable ads to compare
                continue
            # benchmark uses the metric most ads share; pick majority metric
            names = [m[0] for _,m in judged]
            metric_name = max(set(names), key=names.count)
            cohort = [a for a,m in judged if m[0]==metric_name]
            bench = blended(cohort, metric_name)
            if not bench: continue
            benchmarks[f"{slug}/{ch}"] = {"metric": metric_name, "value": round(bench,4), "n_ads": len(cohort)}
            for a, (mn, val) in judged:
                if mn != metric_name: continue
                ratio = val/bench if bench else None
                spend = round(float(a.get("spend",0) or 0),2)
                base = {
                    "event_slug": slug, "channel": ch,
                    "ad_id": str(a.get("ad_id","")), "ad_name": a.get("ad_name","?"),
                    "campaign_name": a.get("campaign_name",""),
                    "spend": spend, "metric_name": mn, "metric_value": round(val,2),
                    "benchmark": round(bench,2), "ratio": round(ratio,2) if ratio else None,
                    "roas": realized.get("roas"),
                }
                # PAUSE: relatively worse AND (for CPL) absolutely worse than the
                # cross-event mean — never flag a strong ad just because peers are stronger.
                abs_bad = True if mn != "CPL" or not mean_cpl else (val > mean_cpl * ABS_PAUSE_MULT)
                if ratio and ratio >= PAUSE_RATIO and abs_bad:
                    base.update({
                        "action": "pause",
                        "suggested_change": "pause / review this ad",
                        "reason": f"{mn} ${val:.2f} — {round((ratio-1)*100)}% גרוע מהממוצע בערוץ (${bench:.2f})"
                                  + (f", וגם מעל הממוצע הכללי (${mean_cpl:.2f})" if (mn=='CPL' and mean_cpl) else "") + ". מבזבז תקציב.",
                    })
                    recs.append(base)
                elif ratio and ratio <= SCALE_RATIO and spend >= SCALE_MIN_SPEND:  # winner w/ real budget
                    base.update({
                        "action": "scale",
                        "suggested_change": f"+{int(SCALE_PCT*100)}% תקציב יומי",
                        "reason": f"{mn} ${val:.2f} — {round((1-ratio)*100)}% טוב מהממוצע בערוץ (${bench:.2f}). מנצח — שווה יותר תקציב.",
                    })
                    recs.append(base)
    # stable id per recommendation
    for r in recs:
        r["id"] = f"{r['event_slug']}|{r['channel']}|{r['ad_id']}|{r['action']}"
        r.setdefault("status", "open")     # open | approved | dismissed | executed
    return recs, benchmarks

def merge_decisions(new_recs, old):
    """Carry over Lauren's decisions (approved/dismissed/executed) onto matching new recs."""
    old_by = {r.get("id"): r for r in (old.get("recommendations") or [])}
    for r in new_recs:
        prev = old_by.get(r["id"])
        if prev and prev.get("status") in ("approved","dismissed","executed"):
            for k in ("status","decided_at","executed_at","execute_result"):
                if k in prev: r[k] = prev[k]
    return new_recs

def main():
    old = {}
    if OUT_PATH.exists():
        try: old = json.loads(OUT_PATH.read_text())
        except Exception: old = {}
    recs, benchmarks = build()
    recs = merge_decisions(recs, old)
    pause = [r for r in recs if r["action"]=="pause"]
    scale = [r for r in recs if r["action"]=="scale"]
    out = {
        "_updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "_about": "@ads-optimizer recommendations. Engine is read-only; execution happens "
                  "only after Lauren approves each rec in /ads-optimizer/. Decisions are "
                  "merge-preserved across runs (IRON RULE #7).",
        "thresholds": {"MIN_SPEND":MIN_SPEND,"PAUSE_RATIO":PAUSE_RATIO,"SCALE_RATIO":SCALE_RATIO,
                       "SCALE_PCT":SCALE_PCT,"ACTIVE_DAYS":ACTIVE_DAYS},
        "benchmarks": benchmarks,
        "summary": {"pause_count":len(pause),"scale_count":len(scale),
                    "events_covered": len({r["event_slug"] for r in recs}),
                    "open_count": len([r for r in recs if r["status"]=="open"])},
        "recommendations": sorted(recs, key=lambda r:(r["action"]!="pause", -r["spend"])),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"✓ {len(pause)} pause + {len(scale)} scale recs across {out['summary']['events_covered']} events → {OUT_PATH}")

if __name__ == "__main__":
    main()

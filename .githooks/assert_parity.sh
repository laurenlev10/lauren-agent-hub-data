#!/usr/bin/env bash
# .githooks/assert_parity.sh — pre-cp size-parity guard. Use this in agent deploy
# scripts BEFORE you `cp <workspace>/Scheduled/outputs/X.html → docs/.../index.html`.
# Catches OneDrive sync truncation before the cp happens, so the repo's index.html
# is never overwritten with a materially smaller (likely truncated) source.
#
# Added 2026-05-08 after a found-in-the-wild truncation: outputs/agent_hub.html
# was 20 KB on Lauren's OneDrive while docs/index.html on GitHub was 75 KB. A
# routine deploy would have wiped 75% of the live dashboard.
#
# Usage:
#   ./.githooks/assert_parity.sh <src> <dst>
#   src = absolute path under /Scheduled/outputs/
#   dst = absolute path under the repo's docs/
#
# Exit 0 = parity OK (or first deploy / dst missing). Exit 1 = src too small,
# DO NOT cp.
set -uo pipefail

SRC="${1:-}"
DST="${2:-}"
THRESHOLD_PCT="${ASSERT_PARITY_THRESHOLD_PCT:-95}"   # allow up to 5% shrink by default

if [ -z "$SRC" ] || [ -z "$DST" ]; then
  echo "Usage: $0 <src> <dst>"
  exit 2
fi

if [ ! -f "$SRC" ]; then
  echo "❌ assert_parity: src does not exist: $SRC"
  exit 1
fi

# First deploy of this dst is fine — no parity to check.
if [ ! -f "$DST" ]; then
  echo "✓ assert_parity: $DST does not exist yet (first deploy) — skipping check."
  exit 0
fi

SRC_SIZE=$(wc -c < "$SRC")
DST_SIZE=$(wc -c < "$DST")
MIN_ALLOWED=$(( DST_SIZE * THRESHOLD_PCT / 100 ))

if [ "$SRC_SIZE" -lt "$MIN_ALLOWED" ]; then
  pct=$(( SRC_SIZE * 100 / DST_SIZE ))
  echo "❌ assert_parity: ABORT — refusing to overwrite a larger deployed file with a smaller source."
  echo "   src=$SRC ($SRC_SIZE B)"
  echo "   dst=$DST ($DST_SIZE B)"
  echo "   src is ${pct}% of dst — threshold is ${THRESHOLD_PCT}%."
  echo "   Likely OneDrive sync truncation. Edit $DST directly in the repo instead."
  exit 1
fi

echo "✓ assert_parity: $SRC ($SRC_SIZE B) ≥ $((THRESHOLD_PCT))% of $DST ($DST_SIZE B) — safe to cp."
exit 0

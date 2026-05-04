#!/usr/bin/env bash
# .githooks/check.sh — on-demand validator. Same checks as pre-commit but for
# arbitrary files. Use BEFORE git add to catch issues early, or as a smoke test.
#
# Usage:
#   ./.githooks/check.sh docs/launch/index.html docs/index.html
#   ./.githooks/check.sh $(git diff --name-only docs/)   # all unstaged files in docs/
#
# Exit code 0 = all files pass. Non-zero = at least one file failed.
set -uo pipefail

if [ $# -eq 0 ]; then
  echo "Usage: $0 <file> [file2 file3 ...]"
  echo "       Runs the same checks as .githooks/pre-commit on the given files."
  exit 2
fi

fail=0
TMPJS=$(mktemp --suffix=.js)
trap "rm -f $TMPJS /tmp/_nodecheck_err" EXIT

count_of() {
  local n
  n=$(grep -c -- "$2" "$1" 2>/dev/null) || n=0
  echo "$n"
}

for f in "$@"; do
  if [ ! -f "$f" ]; then
    echo "❌ $f: file does not exist"
    fail=1
    continue
  fi
  case "$f" in
    *.html) ;;
    *) echo "⏭  $f: not an HTML file, skipping"; continue ;;
  esac

  echo "── checking $f ──"

  # 1. Closing tags
  body=$(count_of "$f" '</body>')
  html=$(count_of "$f" '</html>')
  if [ "$body" != "1" ] || [ "$html" != "1" ]; then
    echo "  ❌ missing closing tags (</body>=$body, </html>=$html). Likely truncation."
    fail=1
  else
    echo "  ✓ closing tags present"
  fi

  # 2. <script> open/close balance
  open_inline=$(grep -oE '<script[^>]*>' "$f" 2>/dev/null | grep -cv 'src=' 2>/dev/null || echo 0)
  close=$(count_of "$f" '</script>')
  if [ "$close" -lt "$open_inline" ] 2>/dev/null; then
    echo "  ❌ script tag imbalance (inline opens=$open_inline, closes=$close)"
    fail=1
  else
    echo "  ✓ script tags balanced ($open_inline inline, $close closes)"
  fi

  # 3. Inline JS passes node --check
  python3 - "$f" "$TMPJS" <<'PY'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, 'r', encoding='utf-8') as f: s = f.read()
js = '\n;\n'.join(re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>', s, re.S))
with open(dst, 'w', encoding='utf-8') as f: f.write(js)
PY
  if [ -s "$TMPJS" ]; then
    if node --check "$TMPJS" 2>/tmp/_nodecheck_err; then
      echo "  ✓ inline JS passes node --check"
    else
      echo "  ❌ inline JS fails node --check:"
      sed 's/^/      /' /tmp/_nodecheck_err
      fail=1
    fi
  else
    echo "  ⏭  no inline JS to check"
  fi

  # 4. Size-shrink heuristic vs HEAD (only meaningful inside the repo)
  if git -C "$(dirname "$f")" rev-parse --git-dir >/dev/null 2>&1; then
    relpath=$(cd "$(dirname "$f")" && git ls-files --full-name "$(basename "$f")" 2>/dev/null)
    if [ -n "$relpath" ] && git cat-file -e "HEAD:$relpath" 2>/dev/null; then
      old=$(git cat-file -s "HEAD:$relpath")
      new=$(stat -c%s "$f" 2>/dev/null || wc -c < "$f")
      if [ "$old" -gt 0 ] && [ $((new * 100)) -lt $((old * 95)) ] 2>/dev/null; then
        echo "  ❌ shrunk from $old → $new bytes (>5%). Likely truncation."
        fail=1
      else
        echo "  ✓ size sane ($old → $new bytes)"
      fi
    fi
  fi
done

echo
if [ $fail -ne 0 ]; then
  echo "✗ At least one file failed. Fix issues before committing."
  exit 1
fi
echo "✓ All files pass guardrails."

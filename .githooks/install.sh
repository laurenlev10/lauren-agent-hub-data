#!/usr/bin/env bash
# Activate the repo's pre-commit guardrails for this clone.
# Every agent that clones lauren-agent-hub-data MUST run this once.
# It tells git to use .githooks/* instead of .git/hooks/* (which is per-clone, untracked).
set -e
git config core.hooksPath .githooks
echo "✓ pre-commit guardrails active (core.hooksPath=.githooks)"
echo "  Try a commit — if any docs/**/*.html is truncated/malformed, commit will be blocked."

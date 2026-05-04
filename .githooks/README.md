# .githooks — pre-commit guardrails

This folder holds **versioned** git hooks for the repo. Standard `.git/hooks/` is per-clone and not versioned, so agents in fresh sessions wouldn't get the protection. We bypass that by setting `git config core.hooksPath .githooks`.

## What it does

`pre-commit` blocks any `git commit` that would land a malformed `docs/**/*.html` file. Specifically it checks:

1. `</body>` and `</html>` each appear exactly once (catches truncation).
2. `<script>` open/close balance — no orphan inline-script opens.
3. `node --check` passes on extracted inline JS — catches syntax errors from bad edits.
4. File hasn't shrunk >5% vs HEAD — heuristic for silent truncation by edit tools.

## Why it exists

On 2026-05-04, commit `1b8fb9f` truncated `docs/launch/index.html` mid-comment, removing the bootstrap that calls `renderSchedule()`. The deployed dashboard rendered with 0 events for ~24 hours before anyone noticed. This hook would have blocked that commit before it landed.

## How to enable in a new clone

```bash
./.githooks/install.sh
```

Equivalent to running `git config core.hooksPath .githooks`. **Every agent that clones this repo must run this immediately after clone.** The `agent-architect` skill documents this as an IRON RULE.

## Bypassing (rare)

If you genuinely need to commit a file that fails the checks (e.g. mid-refactor):

```bash
git commit --no-verify -m "..."
```

Use sparingly. The GitHub Action `validate-dashboards.yml` runs the same checks post-push as a backstop.

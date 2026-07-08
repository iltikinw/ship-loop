---
name: ship-loop
description: Automate a repository's feature, fix, or improvement shipping loop from planning handoff or direct brief through repo-owned plan creation, deterministic ticket implementation, per-ticket commits, resume handling, final Codex review, and optional PR publication. Use when a repo or explicit workspace provides agent-prompts/plan-structure.md and agent-prompts/ticket-implementation.md and the user asks to finalize a plan, run a ticket loop, resume a blocked loop, review completed loop work, or run the standard autonomous shipping process.
---

# Ship Loop

## Required Reading

Before running or resuming a ship loop, read every reference file that applies to the requested mode and phase. These files contain the previous detailed `SKILL.md` contract moved verbatim by topic and remain authoritative for workflow, stop conditions, status handling, resume behavior, final review, and helper usage.

- `references/workspace-mode.md`
- `references/ticket-loop.md`
- `references/status-server.md`
- `references/resume.md`
- `references/final-review.md`

## Public Config Update

This public version adds per-context config at `agent-prompts/ship-loop.json` in the regular repo root or workspace root:

```json
{
  "codex_bin": null,
  "model": "gpt-5.5",
  "reasoning_effort": "high",
  "repo_display_suffixes": [],
  "status_open_browser": true
}
```

CLI arguments override this config. Missing fields use public defaults. This config layer supersedes only hardcoded local binary/display defaults from the legacy contract; all operational behavior remains governed by the topic references listed above.

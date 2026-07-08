# Resume

## Resume Mode

Use resume mode when the user asks to continue a blocked or interrupted ship loop.

Use the explicit run state file printed by the helper:

```text
SHIP_LOOP_STATE /absolute/path/to/state.json
```

If the user does not provide the state path, derive it from the plan owner worktree's git metadata path and the plan slug:

```bash
git -C [planning-repo-worktree] rev-parse --git-path ship-loop/[plan-slug]/state.json
```

State is advisory for progress and parent-agent visibility. Git remains authoritative for completed work. On resume, reconcile:

1. the durable `state.json`;
2. the plan path and `extract_tickets.py` output;
3. `git log --format=%s` ticket commit messages in each target repo;
4. current `git status --short` in each relevant worktree;
5. in workspace mode, the workspace prompt and each target repo's branch/worktree state.

Existing durable child result JSON may be adopted only after schema and semantic validation pass. If a durable implementation or audit/repair result is invalid, mismatches the ticket or target repo, reports invalid confidence/breakdown metadata, or otherwise fails the same validation required for a fresh child result, archive it beside the original result path with an `.invalid-[timestamp]` suffix, emit `invalid_adopted_result`, and rerun that same child stage. Do not keep re-adopting an invalid durable result on resume.

Resume rules:

1. Run `scripts/run_ticket_loop.py --summary --state-file [state-file]` first and inspect consistency warnings, dirty repos, blocker, active child, log age, and result-file status.
2. Invoke `scripts/run_ticket_loop.py` with `--resume --state-file [state-file]` and the same plan, target repo, prompt, slug, and add-dir arguments used for the original run. Resume mode serves a localhost status page by default and reuses the persisted status port when available; include `--daemon` when the resumed helper should keep running after the parent exits.
3. If the state file is missing, stop unless the user explicitly asks to reconstruct from git.
4. If there is dirty work in more than one target repo, stop.
5. If there is dirty work in one target repo, it must cleanly match the state file's current active ticket. If the state is blocked, stale, or ambiguous, resume only with `--adopt-dirty-ticket [ticket-id]` after verifying the dirty work belongs to that ticket.
6. Continue the first ticket that lacks a completed commit or that previously failed the confidence gate.
7. Do not rerun tickets that already have completed commits unless the user explicitly asks for a repair/refactor.
8. Do not skip required Codex CLI implementation or audit agents.
9. Keep writing compact `SHIP_LOOP_EVENT` lines and child-agent logs under the existing run state directory.
10. A blocked helper with status serving enabled should keep its previous localhost status URL alive in read-only hold mode. Still report the printed `SHIP_LOOP_STATUS_URL` on every resume, because older helpers, port conflicts, or externally killed processes may change the actual URL.

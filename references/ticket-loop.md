# Ticket Loop

## Required Tooling

Use subagents before editing code or plans when the repository instructions require subagents or when the available tool surface supports useful delegation. If the repo requires a specific model or reasoning level for subagents, follow that repo instruction.

The parent agent may create the plan, commit the plan, and launch deterministic helper scripts. The parent agent must not edit target application code as part of the ship-loop. Ticket code changes must be made by Codex CLI agents launched by the deterministic loop. After the plan commit, the parent agent must not manually implement ticket code, patch around helper failures, or manually run the ticket loop.

Use the deterministic ticket loop helper to run ticket implementation, audit/repair, verification, and commit checkpoints. The helper must invoke Codex CLI agents for implementation and audit/repair work. Ticket-loop Codex CLI agents must use `gpt-5.5` with `model_reasoning_effort="high"`. If Codex CLI is missing, cannot run a required agent, or cannot satisfy the required model/reasoning pair, stop and report the exact blocker. Do not fall back to parent-agent implementation.

The default Codex binary is the npm-global native binary. Do not rely on a Homebrew binary.

Use available Codex thread/subagent tools for plan context discovery before the plan is written. After plan commit, use Codex CLI through the deterministic loop for:

1. ticket implementation agents;
2. post-ticket audit/repair agents;
3. final review agents when supported by the loop or explicit review command.

If repository instructions require ticket agents and no tool can create a ticket agent with access to the target worktree, stop. Do not silently implement tickets in the parent session as a replacement.

## Parent Orchestrator Boundary

The parent agent owns only:

1. intake and mode resolution;
2. workspace, repo, branch, and worktree setup;
3. plan creation and plan commit;
4. ticket extraction validation before starting the loop;
5. launching the deterministic ticket loop;
6. reporting loop results and blockers.

The parent agent must not:

1. implement ticket code directly;
2. manually substitute for a failed ticket agent;
3. manually commit ticket changes outside the deterministic loop;
4. silently skip the Codex CLI implementation or audit/repair agent.
5. edit target repo source files during an active ship-loop unless the user explicitly cancels the loop and asks for a separate manual change.

If the deterministic loop cannot run, stop with the blocker and leave worktrees intact.

## Helper Scripts

The skill bundles deterministic helpers:

- `scripts/extract_tickets.py <plan.md>` validates canonical ticket headings, requires the `Target repo` ticket index column, and prints ticket JSON including `target_repo`.
- `scripts/render_prompt.py <template.md> --set KEY=VALUE...` renders repo prompt templates and fails if placeholders remain.
- `scripts/run_ticket_loop.py <plan.md> ...` runs the deterministic post-plan ticket loop. It parses tickets, renders prompts, writes durable git-metadata run state, creates compact per-ticket packets, invokes `codex exec` in the target worktree for each ticket, writes child-agent transcripts and result JSON to git-metadata log files, emits compact `SHIP_LOOP_EVENT` progress lines, validates the effective Codex runtime banner/config, invokes bounded Codex CLI audit/repair agents, reruns machine-readable proof commands from the plan after implementation and after every audit/repair patch, enforces schema-bound JSON completion/confidence/audit results, runs `git diff --check`, and commits each ticket in its target repo worktree for the plan.
- `scripts/run_final_review.py --repo NAME=WORKTREE --base NAME=BASE ...` runs the post-loop Codex CLI review agents against the plan worktrees, writes review transcripts and result JSON to git-metadata log files when a state file is provided, validates the effective Codex runtime banner/config, supervises review stalls, and prints schema-bound review JSON for the parent agent to relay to the user. When invoked with `--publish-if-clean`, it pushes each reviewed branch and creates or reuses a pull request only after every final review passes.
- `scripts/ship_loop_state.py` owns git-metadata state paths, atomic state writes, JSONL event appends, and compact parent-facing event output.

The skill also bundles JSON schemas for Codex CLI results:

- `schemas/ticket-result.schema.json`
- `schemas/audit-result.schema.json`
- `schemas/audit-repair-result.schema.json`
- `schemas/review-result.schema.json`
- `schemas/run-state.schema.json`
- `schemas/run-event.schema.json`

Use these helpers instead of ad hoc parsing or string replacement.

## Ticket Scope Contract

Before writing or accepting a plan, enforce ticket narrowness. Each ticket must include:

1. `Primary invariant`: exactly one invariant this ticket establishes or preserves.
2. `Touched surfaces`: no more than two major implementation surfaces. Tests do not count. Documentation counts only when it changes repository behavior or authority docs.
3. `Non-goals`: adjacent work the ticket must not absorb.
4. `Follow-up boundary`: adjacent audit findings that become follow-up tickets instead of repair work.

Major implementation surfaces include runtime control flow, provider integration, schema/data model, UI/API surface, legacy deletion, queue/replay/idempotency, and external contract changes.

Do not accept a ticket that combines new runtime behavior, schema changes, legacy deletion, UI/API cleanup, and broad test migration. Split those into dependent tickets. A ticket should be small enough to implement and pass audit within two repair attempts; otherwise the plan is too coarse.

Recovery/control-flow tickets involving retry semantics, cleanup ordering, durable failure accounting, mutation sequencing, post-store recovery, or idempotency may contain only one primary behavior change. Split classification, sequencing, cleanup, durable accounting, and idempotency into separate tickets unless the plan proves those behaviors are inseparable.

### 4. Run Deterministic Ticket Loop

After the plan commit, the parent agent must launch `scripts/run_ticket_loop.py` and let that helper own the ticket implementation loop. The parent agent must not perform the loop manually.

Default to sequential implementation. Parallel execution is not allowed unless `run_ticket_loop.py` explicitly supports it and the plan's ticket index declares independent groups with disjoint write scopes. Even then, integrate and commit one ticket at a time.

The loop helper performs this deterministic sequence:

1. Parse tickets with `extract_tickets.py`.
2. Require a clean target worktree set before starting.
3. Run a tiny schema-bound `codex exec` preflight with the exact configured Codex binary, model, reasoning effort, approval mode, sandbox shape, and add-dir set. The helper must verify macOS quarantine state, the exact launched command, schema output, and any effective runtime banner/config that Codex exposes. If this fails because of quota, rate limit, broken install, missing native binary, quarantined binary, approval-policy mismatch, model mismatch, sandbox mismatch, or invalid schema output, stop before ticket work and report the state/log path. If Codex omits runtime metadata but returns valid schema output from the exact configured command, emit `runtime_metadata_unavailable` and continue.
4. For each ticket in plan order:
   - resolve the target repo:
   - in regular mode, the target is the current repo worktree;
   - in workspace mode, the target is the ticket's `Target repo` worktree.
   - render the ticket prompt with `render_prompt.py`;
   - write a compact durable ticket packet under the run log tree containing the ticket metadata, exact scoped ticket body, expected files/modules, required verification, and helper-owned proof commands;
   - invoke `codex exec` in the target repo worktree for implementation with `schemas/ticket-result.schema.json`;
   - require the ticket agent result JSON to report `status: complete`, matching `ticket_id`, matching `target_repo`, a confidence score at or above the repository gate, a matching confidence breakdown, no blockers, and passing proof commands;
  - rerun the ticket's concrete backticked proof commands in the helper before audit/repair; if no concrete machine-readable command is present, stop instead of relying on the agent's prose;
   - stop if confidence is below the repository gate;
   - verify no non-target repo changed;
  - invoke a combined write-capable `codex exec` audit/repair agent in the target repo worktree with `schemas/audit-repair-result.schema.json`;
  - require every audit/repair finding to classify `scope` as `in_scope`, `adjacent_followup`, or `out_of_scope`;
  - if an audit/repair pass finds `in_scope` findings, the same child agent must repair only those findings and directly coupled code/tests, then return `status: fail` and `patched: true`;
  - rerun helper-owned proof commands after every audit/repair patch before the next audit/repair pass;
  - do not repair `adjacent_followup` or `out_of_scope` findings inside the current ticket; preserve them for follow-up reporting;
  - cap each ticket at three total audit/repair passes;
  - stop early when any audit/repair pass returns `status: pass`, `patched: false`, matching `ticket_id`, matching `target_repo`, and no `in_scope` findings;
  - if the third audit/repair pass finds and patches `in_scope` findings, rerun helper-owned proof commands and allow the ticket to proceed to commit with `final_pass_repaired` recorded in state; do not run a fourth audit and do not expand the plan automatically;
   - run `git diff --check` in the target repo worktree;
   - commit only the completed ticket scope in the target repo worktree created for this plan.

Regular mode command shape:

```bash
python3 /path/to/skill/scripts/run_ticket_loop.py [plan-path] \
  --ticket-template agent-prompts/ticket-implementation.md \
  --workspace-root [repo-worktree-root] \
  --planning-repo-root [repo-worktree-root] \
  --target-repo [repo-name]=[repo-worktree-root] \
  --plan-abbrev [plan-abbrev] \
  --plan-slug [plan-slug] \
  --state-file [state-file] \
  --codex-bin /[path-to-codex-bin]/codex \
  --daemon \
  --serve-status \
  --max-repair-attempts 2 \
  --stale-warning-heartbeats 2 \
  --stale-fail-heartbeats 8 \
  --add-dir [repo-worktree-root]
```

Workspace mode command shape:

```bash
python3 /path/to/skill/scripts/run_ticket_loop.py [plan-path] \
  --ticket-template [planning-repo-worktree]/agent-prompts/ticket-implementation.md \
  --workspace-root [workspace-root] \
  --planning-repo-root [planning-repo-worktree] \
  --target-repo relay-monorepo=[relay-worktree] \
  --target-repo pathways-admissions-visitlane=[pathways-worktree] \
  --plan-abbrev [plan-abbrev] \
  --plan-slug [plan-slug] \
  --state-file [state-file] \
  --codex-bin [path-to-codex-bin]/codex \
  --daemon \
  --serve-status \
  --max-repair-attempts 2 \
  --stale-warning-heartbeats 2 \
  --stale-fail-heartbeats 8 \
  --add-dir [workspace-root] \
  --add-dir [planning-repo-worktree]
```

The helper invokes Codex CLI implementation agents with this required shape:

```bash
codex -a never exec \
  -C [target-repo-root] \
  -m gpt-5.5 \
  -c 'model_reasoning_effort="high"' \
  -s workspace-write \
  --output-schema [schema-path] \
  --output-last-message [durable-result-json] \
  --json \
  --add-dir [needed-readable-or-writable-root] \
  -
```

The helper invokes Codex CLI audit/repair agents with the same schema-bound shape and a write-capable sandbox:

```bash
codex -a never exec \
  -C [target-repo-root] \
  -m gpt-5.5 \
  -c 'model_reasoning_effort="high"' \
  -s workspace-write \
  --output-schema /path/to/skill/schemas/audit-repair-result.schema.json \
  --output-last-message [durable-result-json] \
  --json \
  --add-dir [needed-readable-or-writable-root] \
  -
```

Do not use deprecated approval modes such as `on-failure`.

Do not override the ticket-loop model or reasoning effort. `run_ticket_loop.py` must run with `--model gpt-5.5` and `--reasoning-effort high`; the helper rejects other values. Do not set `--max-repair-attempts` above `2`; future runs use at most three total audit/repair passes per ticket and must not run a fourth audit.

The parent agent may inspect compact helper stdout/stderr, the state file, and referenced log files when needed to report blockers, but must not repair or bypass helper failures manually.

If Codex returns a quota, rate-limit, retry-after, or usage-limit blocker, persist state, report the blocker and log path, and stop. Do not keep the parent thread alive waiting through a reset window.

If the helper emits `possibly_stalled`, report that status only if the user asks or the helper later blocks. If the helper emits `stale_child`, stop and report the blocker, PID, log path, result path, and dirty worktree status.

For status checks while the loop is running or after interruption, use:

```bash
python3 /path/to/skill/scripts/run_ticket_loop.py --status --state-file [state-file]
```

Prefer `--summary` over `--status` after blockers or interruptions because it includes consistency warnings and active-child progress metadata.

Ticket commit message:

```txt
[PLAN-ABBREV] [TICKET-ID]: [ticket title]
```

Use a concise plan abbreviation derived from the plan slug. If no obvious abbreviation exists, use the branch prefix and slug, e.g. `feat-my-plan ABC-01: title`.

### 5. Stop Conditions

Stop without inventing a fallback when:

1. a required authority doc or prompt template is missing;
2. the plan cannot be parsed by `extract_tickets.py`;
3. a ticket agent cannot be created when repository instructions require one;
4. a ticket reports confidence below the required gate;
5. required tests or proof commands fail;
6. the codebase contradicts the plan or repository instructions;
7. final review finds unresolved correctness, security, contract, or repository-instruction violations.
8. workspace mode was requested but `agent-prompts/workspace-mode.md` is missing or inconsistent;
9. a workspace ticket omits `Target repo`, names an unallowed target repo, or targets multiple repos;
10. a workspace ticket writes VisitLane planning docs, architecture docs, PRDs, implementation plans, decision records, handoff notes, or internal design notes under a repo that the workspace prompt forbids;
11. a workspace ticket changes a repo other than its declared target repo.
12. `scripts/run_ticket_loop.py` is missing, fails, or cannot invoke `codex exec`;
13. a ticket agent result is missing, not durable under the run log tree, invalid against `schemas/ticket-result.schema.json`, mismatches the ticket or target repo, reports blockers, reports failed proofs, or has confidence below the repository gate;
14. an audit/repair agent result is missing, invalid against `schemas/audit-repair-result.schema.json`, mismatches the ticket or target repo, reports `patched: true` without `in_scope` findings, reports `status: pass` with `in_scope` findings, omits `scope` on any finding, changes files while reporting `patched: false`, or reports blockers, failed proofs, or confidence below the repository gate after a patch.
15. an audit/repair pass reports `status: fail` and `patched: false`, because that means the ticket cannot be repaired deterministically inside the capped loop.
16. the helper reports state consistency warnings that cannot be reconciled without user input.
17. the helper reports `stale_child` for a long-running implementation, audit/repair, or review child.
18. the helper reports `runtime_mismatch`, `approval_prompt`, `macos_quarantine`, `missing_proof_command`, or `proof_failed`.
19. clean-review publishing fails because `gh` is unavailable, the current worktree is detached, a push fails, a PR cannot be created or found, or an explicit `--pr-base` is missing.

On stop, leave the branch and worktree intact. Report:

1. current branch and worktree;
2. plan path;
3. completed ticket commits;
4. active blocked ticket;
5. exact blocker;
6. minimum next checks needed to resume.

If the third audit/repair pass patches findings, do not run a fourth audit and do not add tickets automatically. Commit only after helper-owned proofs and `git diff --check` pass; final review is the next independent review gate.

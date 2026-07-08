# Status Server

## Run State And Progress Output

Every post-plan loop must use durable run state outside repo contents. Store state under the plan owner worktree's git metadata path:

```bash
git -C [planning-repo-worktree] rev-parse --git-path ship-loop/[plan-slug]/state.json
```

The sibling event and log locations are:

```text
[git-metadata]/ship-loop/[plan-slug]/events.jsonl
[git-metadata]/ship-loop/[plan-slug]/logs/
```

Child result JSON must be durable under the same log tree, never only in `/tmp` or `$TMPDIR`:

```text
[git-metadata]/ship-loop/[plan-slug]/logs/[ticket-id]/implementation-0.result.json
[git-metadata]/ship-loop/[plan-slug]/logs/[ticket-id]/audit-repair-1.result.json
[git-metadata]/ship-loop/[plan-slug]/logs/[ticket-id]/audit-repair-2.result.json
[git-metadata]/ship-loop/[plan-slug]/logs/[ticket-id]/audit-repair-3.result.json
[git-metadata]/ship-loop/[plan-slug]/logs/review/[repo].result.json
```

When status serving is enabled, runtime status UI metadata lives outside tracked repo contents:

```text
[workspace-root]/.ship-loop/[plan-slug]/
[repo-root]/.ship-loop/[plan-slug]/
```

Use the workspace-root path in workspace mode and the repo-root path in regular mode. The helper should add the repo-level `.ship-loop/[plan-slug]/` path to `.git/info/exclude` when it is inside a Git worktree.

Status serving must be singleton per run state file. When a new localhost status server starts, it must write a current status-server generation record and append it to a local registry under the runtime status directory. It must then retire older status servers for the same `state.json`: first by calling their local close endpoint when available, then by terminating only a verified `run_ticket_loop.py --serve-status` process whose command line names the same state file. Older fixed status servers must also exit their hold loop when they observe that their generation is no longer current. Do not auto-close a completed page solely because the run completed; the user may close it with the Close Page button.

Status serving should keep a sticky localhost port per run by default. If the user passes `--status-port`, persist that port in `state.json` launch metadata. If the helper auto-selects a free port, persist the bound port after the status server starts and include that port in the stored `launch.resume_command`. On resume, retire any older status server for the same state file before binding a fixed sticky port so the resumed page can reuse the same URL when the port is available.

The status page UI must be read from `scripts/status_page_template.html` on every `GET /`. Keep HTML, CSS, and browser-side JavaScript in that template so template edits are visible after a browser refresh without restarting the status server. The Python helper should inject only the current status payload into the template and keep `/state.json` polling as the live data path.

Each active helper run must hold a per-state lock at `[git-metadata]/ship-loop/[plan-slug]/run.lock`. If the lock is held by a live helper, do not start another helper for the same plan. When a helper with status serving enabled blocks or records a failed ticket, it must release this run lock before switching into read-only status-page hold mode.

The parent agent must report the `SHIP_LOOP_STATE` path as soon as the helper prints it. The parent agent should not expect child-agent transcripts on stdout. The helper's stdout is an index, not a transcript.

Parent-facing progress output must stay compact:

```text
SHIP_LOOP_STATE /absolute/path/to/state.json
SHIP_LOOP_STATUS_URL http://127.0.0.1:PORT/
SHIP_LOOP_EVENT {"phase":"ticket_loop","ticket_id":"ABC-01","stage":"implementation","status":"started","log":"/absolute/path/to/log"}
SHIP_LOOP_EVENT {"phase":"ticket_loop","ticket_id":"ABC-01","stage":"commit","status":"complete","commit":"abc123"}
```

Implementation, audit/repair, and review agent stdout/stderr must be written to log files under the run's git-metadata `logs/` directory. The parent agent may read a log file only when it needs details for a blocker or user-requested status report. On failure, report the compact blocker and log path instead of pasting the full transcript.

While a child Codex CLI agent is running, emit rate-limited heartbeat events, defaulting to one compact event every 180 seconds. Do not stream child-agent stdout/stderr into parent stdout by default. The helper should use Codex JSON events when available, copy the full child stream into the log file, and expose only compact heartbeat fields such as PID, elapsed seconds, inactive seconds, current tool when known, log path, result path, and last structured event type.

The helper must validate the effective Codex runtime when Codex exposes runtime metadata, not only the intended command line. Preflight and every child agent must fail hard if the runtime banner or structured config reports the wrong model, reasoning effort, approval policy, sandbox, or workdir. A mismatch such as passing `-a never` while Codex reports `approval: on-request` is a hard blocker. Current Codex JSON event output may omit runtime metadata entirely; missing runtime metadata alone is not a blocker after the helper has launched the exact configured command and received valid schema output. In that case, emit an explicit `runtime_metadata_unavailable` event and continue.

If the helper detects an interactive approval prompt, a quarantined Codex binary, missing native binary, missing durable result file, or a child process with no structured/log/result progress past the stale threshold, it must terminate the child process group, persist a blocker, and leave dirty work intact for resume.

The owning parent agent should poll less by treating the helper as the active worker. After launching `run_ticket_loop.py`, the parent should wait on the long-running helper command instead of repeatedly inspecting worktrees or logs. The parent should read `--status` or a referenced log only when:

1. the helper exits;
2. the user asks for status;
3. no heartbeat or output appears after the configured heartbeat interval plus a reasonable grace period;
4. a compact event reports `failed`, `blocked`, or a log path the parent must summarize.

The parent must not poll by repeatedly running git status, reading child-agent logs, or inspecting target code while a ticket agent is still running.

For lowest parent-token usage, launch the ticket loop in detached status-page mode:

```bash
python3 /path/to/skill/scripts/run_ticket_loop.py [plan-path] ... --daemon --serve-status
```

The daemon launch prints `SHIP_LOOP_STATE`, `SHIP_LOOP_STATUS_URL`, and `SHIP_LOOP_DAEMON_PID`, then returns control to the parent. The child helper continues independently. Daemon mode serves the status page by default. When status serving is enabled, the helper starts a read-only localhost page and opens it in the default browser once per run. Use `--no-open-browser` only for headless or remote sessions. Use `--no-serve-status` only when a localhost server is impossible. If browser opening fails, do not fail the ship-loop; report the URL and record a non-blocking status warning.

The localhost status page may show the current phase, ticket table, active child PID/log age, last event, blocker, state path, and status counts. It also exposes three local controls: pause, resume, and close page. These controls must not mutate code, commit, push, create PRs, run final review, or bypass the deterministic helper. They may only write run-control state, terminate an active child process group for pause, launch the persisted deterministic resume command, or stop an inactive/held localhost status server.

The pause control is cooperative. It records `requested_action: "pause"` in `state.json`, terminates the active Codex child process group when one is running, and the helper must then persist `phase: "paused"` and ticket `status: "paused"` at the next controlled boundary while leaving dirty work intact. A pause is not a failure and should be resumed through the helper.

The resume control is available after `blocked`, `failed`, or `paused` states. It must launch the stored `launch.resume_command` from `state.json`, add `--resume`, serve a fresh or existing localhost status page, and add `--adopt-dirty-ticket [ticket-id]` when the current ticket owns dirty work. It must reject resume while a child is already active or when the run is not in a resumable state.

The close page control is for cleanup after the user no longer needs the localhost page, especially after completion or after inspecting a held blocker/paused page. It must reject close while a child is active or the loop is mid-flight. On accepted close, it should append a `close_requested` event and stop only the localhost status server/held status process.

When status serving is enabled, a blocker or pause must not crash or close the localhost page. After persisting the blocker, failed ticket state, or paused state, the helper should keep serving the status page in a status-only hold mode and release the run lock so a later resume can start. The hold process may be stopped separately after the user no longer needs the page.

For analytics on an active or interrupted loop, use:

```bash
python3 /path/to/skill/scripts/run_ticket_loop.py --summary --state-file [state-file]
```

The summary should be preferred over manually sampling logs because it reports ticket status, stage counts, current work, and blocked/failed tickets without adding child-agent transcript noise to the parent context.

The summary also reports active child PID, result path, log modification age, result-file status, dirty repos recorded in state, blocker state, consistency warnings, event status counts, stage timing analytics, recent stall events, and quota events. Use `--summary --json` when a machine-readable status report is useful. After any blocker, interruption, or user status question, run `--summary` before deciding whether to resume.

If the user asks to stop one active ticket agent, use the structured stop command instead of ad hoc process killing:

```bash
python3 /path/to/skill/scripts/run_ticket_loop.py --stop --state-file [state-file] --ticket [ticket-id]
```

The stop command must preserve dirty work, record `stopped_by_user`, and leave the run resumable.

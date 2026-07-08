#!/usr/bin/env python3
import argparse
import json
import os
import re
import selectors
import shlex
import subprocess
import sys
import time
from pathlib import Path

from ship_loop_state import append_event, logs_root, now_iso, read_state, update_phase, write_state
from run_ticket_loop import (
    APPROVAL_PROMPT_PATTERNS,
    RUNTIME_BANNER_KEYS,
    assert_no_quarantine,
    child_cpu_seconds,
    file_age_seconds,
    file_mtime_iso,
    parse_runtime_banner,
    default_codex_bin,
    resolve_codex_bin,
    terminate_process_group,
    validate_runtime_banner,
)
from ship_loop_config import ShipLoopConfigError, load_ship_loop_config


QUOTA_PATTERNS = [
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"retry after", re.IGNORECASE),
    re.compile(r"try again after", re.IGNORECASE),
    re.compile(r"quota", re.IGNORECASE),
]


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def config_root_from_state(state_file: Path | None) -> Path | None:
    if state_file is None or not state_file.is_file():
        return None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    root = state.get("workspace_root") or state.get("planning_repo_root")
    if not isinstance(root, str) or not root:
        return None
    return Path(root)


def run(command: list[str], *, cwd: Path | None = None, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, input=stdin, text=True, capture_output=True, check=False)


def require_success(result: subprocess.CompletedProcess[str], description: str) -> None:
    if result.returncode == 0:
        return
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    fail(f"{description} failed with exit code {result.returncode}")


def command_text(command: list[str]) -> str:
    return shlex.join(command)


def parse_assignment(value: str, flag: str) -> tuple[str, str]:
    if "=" not in value:
        fail(f"invalid {flag} value {value!r}; expected NAME=VALUE")
    name, assigned = value.split("=", 1)
    if not name or not assigned:
        fail(f"invalid {flag} value {value!r}; expected NAME=VALUE")
    return name, assigned


def schema_path() -> Path:
    path = Path(__file__).parents[1] / "schemas" / "review-result.schema.json"
    if not path.is_file():
        fail(f"schema file not found: {path}")
    return path


def require_linked_worktree(name: str, root: Path) -> None:
    git_file = root / ".git"
    if not git_file.is_file():
        fail(f"review repo {name} must be a linked worktree with a .git file, not the original repo root: {root}")
    result = run(["git", "-C", str(root), "rev-parse", "--show-toplevel"])
    require_success(result, f"git rev-parse in {root}")
    top_level = Path(result.stdout.strip()).resolve()
    if top_level != root:
        fail(f"review repo {name} top-level mismatch: expected {root}, got {top_level}")


def git_status(repo: Path) -> str:
    result = run(["git", "-C", str(repo), "status", "--short"])
    require_success(result, f"git status in {repo}")
    return result.stdout


def git_value(repo: Path, args: list[str], description: str) -> str:
    result = run(["git", "-C", str(repo), *args])
    require_success(result, f"{description} in {repo}")
    return result.stdout.strip()


def resolve_base_sha(repo: Path, base: str) -> str:
    result = run(["git", "-C", str(repo), "rev-parse", "--verify", f"{base}^{{commit}}"])
    require_success(result, f"git rev-parse base {base} in {repo}")
    return result.stdout.strip()


def diff_check(repo: Path, base_sha: str) -> None:
    result = run(["git", "-C", str(repo), "diff", "--check", f"{base_sha}...HEAD"])
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    require_success(result, f"git diff --check {base_sha}...HEAD in {repo}")


def require_tool(name: str) -> None:
    result = run([name, "--version"])
    if result.returncode != 0:
        fail(f"required tool not available for publish step: {name}")


def publish_log_path(state_file: Path | None, repo_name: str) -> Path:
    if state_file:
        return logs_root(state_file) / "publish" / f"{repo_name}.log"
    return Path(f"{repo_name}.publish.log").resolve()


def append_publish_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def run_publish_command(command: list[str], cwd: Path, log_path: Path, description: str) -> subprocess.CompletedProcess[str]:
    append_publish_log(log_path, f"$ {command_text(command)}\n")
    result = run(command, cwd=cwd)
    append_publish_log(log_path, result.stdout)
    append_publish_log(log_path, result.stderr)
    append_publish_log(log_path, f"exit {result.returncode}\n\n")
    if result.returncode != 0:
        fail(f"{description} failed; log: {log_path}")
    return result


def existing_pr_url(repo_root: Path, branch: str, log_path: Path) -> str | None:
    result = run_publish_command(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--limit", "1"],
        repo_root,
        log_path,
        f"gh pr list for {branch}",
    )
    try:
        parsed = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        fail(f"gh pr list returned invalid JSON for {branch}: {exc}; log: {log_path}")
    if parsed:
        url = parsed[0].get("url")
        if isinstance(url, str) and url:
            return url
    return None


def pr_title(args: argparse.Namespace, repo_name: str, multi_repo: bool) -> str:
    if args.pr_title_prefix:
        prefix = args.pr_title_prefix
    elif args.plan:
        prefix = args.plan.stem
    else:
        prefix = "ship-loop"
    return f"{prefix}: {repo_name}" if multi_repo else prefix


def pr_body(args: argparse.Namespace, repo_name: str, branch: str, review: dict[str, object]) -> str:
    lines = [
        "Created by the ship-loop final-review helper after a clean review.",
        "",
        f"Repository: {repo_name}",
        f"Branch: {branch}",
    ]
    if args.plan:
        lines.append(f"Plan: {args.plan}")
    lines.extend(
        [
            "",
            "Final review status: pass",
            f"Residual risks: {json.dumps(review.get('residual_risks', []), ensure_ascii=False)}",
        ]
    )
    return "\n".join(lines)


def publish_repo_pr(
    args: argparse.Namespace,
    repo_name: str,
    repo_root: Path,
    pr_base: str,
    review: dict[str, object],
    multi_repo: bool,
) -> dict[str, object]:
    if git_status(repo_root).strip():
        fail(f"cannot publish dirty worktree for {repo_name}: {repo_root}")
    branch = git_value(repo_root, ["branch", "--show-current"], "git branch")
    if not branch:
        fail(f"cannot publish detached HEAD worktree for {repo_name}; a branch is required for PR creation")
    remote = args.publish_remote
    log_path = publish_log_path(args.state_file, repo_name)
    log_path.write_text(f"Started: {now_iso()}\nRepo: {repo_name}\nBranch: {branch}\nRemote: {remote}\nBase: {pr_base}\n\n", encoding="utf-8")
    if args.state_file:
        append_event(
            args.state_file,
            {
                "phase": "publish",
                "repo": repo_name,
                "status": "started",
                "branch": branch,
                "remote": remote,
                "base": pr_base,
                "log": str(log_path),
            },
        )
    run_publish_command(["git", "push", "-u", remote, branch], repo_root, log_path, f"git push for {repo_name}")
    url = existing_pr_url(repo_root, branch, log_path)
    created = False
    if url is None:
        result = run_publish_command(
            [
                "gh",
                "pr",
                "create",
                "--head",
                branch,
                "--base",
                pr_base,
                "--title",
                pr_title(args, repo_name, multi_repo),
                "--body",
                pr_body(args, repo_name, branch, review),
            ],
            repo_root,
            log_path,
            f"gh pr create for {repo_name}",
        )
        url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if not url:
            fail(f"gh pr create did not return a PR URL for {repo_name}; log: {log_path}")
        created = True
    result = {
        "repo": repo_name,
        "status": "created" if created else "existing",
        "branch": branch,
        "remote": remote,
        "base": pr_base,
        "url": url,
        "log": str(log_path),
    }
    if args.state_file:
        append_event(args.state_file, {"phase": "publish", "repo": repo_name, "status": result["status"], "url": url, "log": str(log_path)})
    return result


def is_quota_blocker(log_path: Path) -> bool:
    if not log_path.is_file():
        return False
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return any(pattern.search(text) for pattern in QUOTA_PATTERNS)


def codex_review_command(
    args: argparse.Namespace,
    repo_root: Path,
    base: str,
    output_last_message: Path,
) -> list[str]:
    command = [
        args.codex_bin,
        "-a",
        args.approval_policy,
        "-C",
        str(repo_root),
        "-s",
        "read-only",
    ]
    if args.codex_json_events:
        command.append("--json")
    command.extend([
        "exec",
        "review",
        "--base",
        base,
        "-m",
        args.model,
        "-c",
        f'model_reasoning_effort="{args.reasoning_effort}"',
        "--output-schema",
        str(schema_path()),
        "--output-last-message",
        str(output_last_message),
    ])
    return command


def validate_review_result(result: dict[str, object], repo_name: str) -> None:
    if result.get("repo") != repo_name:
        fail(f"{repo_name} review repo mismatch: {result.get('repo')!r}")
    status = result.get("status")
    if status not in ("pass", "fail"):
        fail(f"{repo_name} review status must be pass or fail, got {status!r}")
    findings = result.get("findings")
    if not isinstance(findings, list):
        fail(f"{repo_name} review findings must be a list")
    if status == "pass" and findings:
        fail(f"{repo_name} review status pass cannot include findings")
    if status == "fail" and not findings:
        fail(f"{repo_name} review status fail must include findings")


def normalize_review_prose(repo_name: str, text: str) -> dict[str, object]:
    if (
        "No actionable correctness issues were found" in text
        or "No actionable correctness issues were identified" in text
        or "no actionable correctness issues" in text.lower()
        or "No actionable correctness, security, or maintainability issues were identified" in text
        or "No actionable findings" in text
        or "No actionable regressions were identified" in text
        or "No actionable regressions were found" in text
        or "No discrete, actionable regressions were identified" in text
        or "No discrete correctness issues were found" in text
        or "No discrete correctness issues were identified" in text
        or "No discrete correctness, security, or maintainability issues were identified" in text
        or "No discrete introduced correctness issues were identified" in text
        or "No discrete, actionable correctness issues were identified" in text
        or "I did not identify any discrete, actionable regressions" in text
        or "I did not identify any discrete, introduced correctness issue" in text
        or "I did not find any discrete, actionable regressions" in text
        or "I did not find any discrete, actionable bugs" in text
        or "I did not find any discrete, actionable correctness issues" in text
    ):
        return {
            "repo": repo_name,
            "status": "pass",
            "findings": [],
            "residual_risks": [
                "Codex review returned prose instead of schema JSON; helper normalized a no-actionable-findings review result."
            ],
        }

    findings: list[dict[str, object]] = []
    pattern = re.compile(
        r"^- \[(P\d+)\]\s+(.*?)\s+—\s+(.+?)(?::(\d+))?\s*$",
        re.MULTILINE,
    )
    severity_by_priority = {
        "P0": "critical",
        "P1": "high",
        "P2": "medium",
        "P3": "low",
    }
    for match in pattern.finditer(text):
        priority, title, file_path, line_text = match.groups()
        description_start = match.end()
        next_match = pattern.search(text, description_start)
        description_block = text[description_start:next_match.start() if next_match else len(text)]
        description = " ".join(
            line.strip()
            for line in description_block.splitlines()
            if line.strip() and not line.strip().startswith("ERROR:")
        )
        findings.append(
            {
                "severity": severity_by_priority.get(priority, "medium"),
                "file": file_path.strip(),
                "line": int(line_text) if line_text else None,
                "description": f"{title.strip()}: {description}".strip(),
                "required_fix": description or title.strip(),
            }
        )

    if findings:
        return {
            "repo": repo_name,
            "status": "fail",
            "findings": findings,
            "residual_risks": [
                "Codex review returned prose instead of schema JSON; helper normalized review-comment findings."
            ],
        }

    fail(f"Codex review for {repo_name} wrote invalid JSON and prose could not be normalized")


def read_review_result_from_log(repo_name: str, output_last_message: Path, log_path: Path) -> dict[str, object]:
    text = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
    if output_last_message.is_file():
        raw = output_last_message.read_text(encoding="utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            text = text + "\n" + raw
    return normalize_review_prose(repo_name, text)


def update_review_state(state_file: Path | None, status: str, repo_name: str, log_path: Path | None = None) -> None:
    if state_file is None:
        return
    state = read_state(state_file)
    state.setdefault("review", {})
    state["review"]["status"] = status
    state["review"]["repo"] = repo_name
    if log_path is not None:
        state["review"]["log"] = str(log_path)
    write_state(state_file, state)


def run_review(args: argparse.Namespace, repo_name: str, repo_root: Path, base: str) -> dict[str, object]:
    base_sha = resolve_base_sha(repo_root, base)
    diff_check(repo_root, base_sha)
    status_before = git_status(repo_root)
    output_last_message = (
        logs_root(args.state_file) / "review" / f"{repo_name}.result.json"
        if args.state_file
        else Path(f"{repo_name}.review-result.json").resolve()
    )
    output_last_message.parent.mkdir(parents=True, exist_ok=True)
    output_last_message.unlink(missing_ok=True)
    log_path = (
        logs_root(args.state_file) / "review" / f"{repo_name}.log"
        if args.state_file
        else Path(f"{repo_name}.review.log").resolve()
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    update_review_state(args.state_file, "running", repo_name, log_path)
    if args.state_file:
        append_event(
            args.state_file,
            {
                "phase": "final_review",
                "repo": repo_name,
                "status": "started",
                "log": str(log_path),
                "result_path": str(output_last_message),
            },
        )
    command = codex_review_command(args, repo_root, base_sha, output_last_message)
    started_at = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write(f"Started: {now_iso()}\n")
        log_file.write(f"Repo: {repo_name}\n")
        log_file.write(f"Command: {command_text(command)}\n\n")
        log_file.flush()
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            bufsize=1,
        )
        selector = selectors.DefaultSelector()
        if process.stdout is not None:
            selector.register(process.stdout, selectors.EVENT_READ)
        next_heartbeat = started_at + args.heartbeat_seconds
        last_progress_at = started_at
        possible_stall_reported = False
        stdout_closed = False
        banner_validated = False
        while process.poll() is None or not stdout_closed:
            events = selector.select(timeout=1)
            for key, _ in events:
                line = key.fileobj.readline()
                if line == "":
                    try:
                        selector.unregister(key.fileobj)
                    except KeyError:
                        pass
                    stdout_closed = True
                    continue
                log_file.write(line)
                log_file.flush()
                last_progress_at = time.monotonic()
                possible_stall_reported = False
                stripped = line.strip()
                if not banner_validated and RUNTIME_BANNER_KEYS.issubset(set(parse_runtime_banner(log_path.read_text(encoding="utf-8", errors="replace")))):
                    mismatches = validate_runtime_banner(
                        log_path,
                        expected_workdir=repo_root,
                        expected_model=args.model,
                        expected_reasoning_effort=args.reasoning_effort,
                        expected_sandbox="read-only",
                        expected_approval=args.approval_policy,
                    )
                    if mismatches:
                        terminate_process_group(process.pid)
                        message = "effective Codex review runtime mismatch: " + "; ".join(mismatches)
                        if args.state_file:
                            update_review_state(args.state_file, "blocked", repo_name, log_path)
                            update_phase(args.state_file, "blocked", blocker={"phase": "final_review", "repo": repo_name, "reason": "runtime_mismatch", "log": str(log_path), "mismatches": mismatches})
                            append_event(args.state_file, {"phase": "final_review", "repo": repo_name, "status": "runtime_mismatch", "log": str(log_path), "message": message})
                        fail(message)
                    banner_validated = True
                if any(pattern.search(stripped) for pattern in APPROVAL_PROMPT_PATTERNS):
                    terminate_process_group(process.pid)
                    message = f"Codex review for {repo_name} reached an interactive approval prompt; log: {log_path}"
                    if args.state_file:
                        update_review_state(args.state_file, "blocked", repo_name, log_path)
                        update_phase(args.state_file, "blocked", blocker={"phase": "final_review", "repo": repo_name, "reason": "approval_prompt", "log": str(log_path), "pid": process.pid})
                        append_event(args.state_file, {"phase": "final_review", "repo": repo_name, "status": "approval_prompt", "log": str(log_path), "message": message})
                    fail(message)

            now = time.monotonic()
            result_exists = output_last_message.is_file()
            if result_exists:
                last_progress_at = now
            if args.state_file and args.heartbeat_seconds > 0 and now >= next_heartbeat:
                inactive_seconds = int(now - last_progress_at)
                heartbeat = {
                    "phase": "final_review",
                    "repo": repo_name,
                    "status": "running",
                    "elapsed_seconds": int(now - started_at),
                    "inactive_seconds": inactive_seconds,
                    "pid": process.pid,
                    "cpu_seconds": child_cpu_seconds(process.pid),
                    "log": str(log_path),
                    "log_mtime": file_mtime_iso(log_path),
                    "log_mtime_age_seconds": file_age_seconds(log_path),
                    "result_path": str(output_last_message),
                    "result_exists": result_exists,
                }
                append_event(args.state_file, heartbeat)
                stale_warning_seconds = args.heartbeat_seconds * args.stale_warning_heartbeats
                stale_fail_seconds = args.heartbeat_seconds * args.stale_fail_heartbeats
                if args.stale_warning_heartbeats > 0 and inactive_seconds >= stale_warning_seconds and not possible_stall_reported:
                    append_event(args.state_file, {**heartbeat, "status": "possibly_stalled"})
                    possible_stall_reported = True
                if args.stale_fail_heartbeats > 0 and inactive_seconds >= stale_fail_seconds:
                    terminate_process_group(process.pid)
                    message = f"Codex review for {repo_name} appears stalled: no review log/result progress for {inactive_seconds}s; log: {log_path}"
                    update_review_state(args.state_file, "blocked", repo_name, log_path)
                    update_phase(
                        args.state_file,
                        "blocked",
                        blocker={"phase": "final_review", "repo": repo_name, "reason": "stale_child", "log": str(log_path), "pid": process.pid, "inactive_seconds": inactive_seconds},
                    )
                    append_event(args.state_file, {**heartbeat, "status": "stale_child", "message": message})
                    fail(message)
                next_heartbeat = now + args.heartbeat_seconds
        returncode = process.returncode
        if not banner_validated:
            mismatches = validate_runtime_banner(
                log_path,
                expected_workdir=repo_root,
                expected_model=args.model,
                expected_reasoning_effort=args.reasoning_effort,
                expected_sandbox="read-only",
                expected_approval=args.approval_policy,
            )
            if mismatches:
                message = "effective Codex review runtime mismatch: " + "; ".join(mismatches)
                if args.state_file:
                    update_review_state(args.state_file, "blocked", repo_name, log_path)
                    update_phase(args.state_file, "blocked", blocker={"phase": "final_review", "repo": repo_name, "reason": "runtime_mismatch", "log": str(log_path), "mismatches": mismatches})
                fail(message)
        log_file.write(f"\nFinished: {now_iso()}\nExit code: {returncode}\n")

    if returncode != 0:
        quota_blocked = is_quota_blocker(log_path)
        message = f"Codex review for {repo_name} failed with exit code {returncode}; log: {log_path}"
        if quota_blocked:
            message = f"Codex review for {repo_name} hit a Codex quota/rate-limit blocker; stop and resume later; log: {log_path}"
        if args.state_file:
            update_review_state(args.state_file, "blocked" if quota_blocked else "failed", repo_name, log_path)
            if quota_blocked:
                update_phase(args.state_file, "blocked", blocker={"phase": "final_review", "repo": repo_name, "reason": "quota", "log": str(log_path)})
            append_event(
                args.state_file,
                {
                    "phase": "final_review",
                    "repo": repo_name,
                    "status": "quota_blocked" if quota_blocked else "failed",
                    "log": str(log_path),
                    "message": message,
                },
            )
        fail(message)
    if git_status(repo_root) != status_before:
        fail(f"read-only final review changed repo status for {repo_name}")
    parsed = read_review_result_from_log(repo_name, output_last_message, log_path)
    validate_review_result(parsed, repo_name)
    if args.state_file:
        update_review_state(args.state_file, parsed["status"], repo_name, log_path)
        append_event(
            args.state_file,
            {
                "phase": "final_review",
                "repo": repo_name,
                "status": parsed["status"],
                "elapsed_seconds": int(time.monotonic() - started_at),
                "log": str(log_path),
            },
        )
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run final Codex CLI reviews for ship-loop plan worktrees.")
    parser.add_argument("--repo", action="append", required=True, metavar="NAME=PATH")
    parser.add_argument("--base", action="append", required=True, metavar="NAME=BASE")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--codex-bin")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--approval-policy", default="never")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--heartbeat-seconds", default=180, type=int)
    parser.add_argument("--stale-warning-heartbeats", default=2, type=int)
    parser.add_argument("--stale-fail-heartbeats", default=8, type=int)
    parser.add_argument("--publish-if-clean", action="store_true")
    parser.add_argument("--publish-remote", default="origin")
    parser.add_argument("--pr-base", action="append", default=[], metavar="NAME=BRANCH")
    parser.add_argument("--pr-title-prefix")
    parser.add_argument("--no-codex-json-events", dest="codex_json_events", action="store_false")
    parser.set_defaults(codex_json_events=True)
    args = parser.parse_args()

    if args.heartbeat_seconds < 0:
        fail("--heartbeat-seconds must be non-negative")
    if args.stale_warning_heartbeats < 0:
        fail("--stale-warning-heartbeats must be non-negative")
    if args.stale_fail_heartbeats < 0:
        fail("--stale-fail-heartbeats must be non-negative")
    if (
        args.stale_fail_heartbeats
        and args.stale_warning_heartbeats
        and args.stale_fail_heartbeats <= args.stale_warning_heartbeats
    ):
        fail("--stale-fail-heartbeats must be greater than --stale-warning-heartbeats")
    if args.state_file:
        args.state_file = args.state_file.resolve()
        update_phase(args.state_file, "final_review")
    try:
        config = load_ship_loop_config(args.config_root or config_root_from_state(args.state_file))
    except ShipLoopConfigError as exc:
        fail(str(exc))
    args.codex_bin = args.codex_bin or config.codex_bin or default_codex_bin()
    args.model = args.model or config.model
    args.reasoning_effort = args.reasoning_effort or config.reasoning_effort
    args.codex_bin = resolve_codex_bin(args.codex_bin)
    assert_no_quarantine(args.codex_bin, args.state_file)

    repos = {name: Path(path).resolve() for name, path in (parse_assignment(value, "--repo") for value in args.repo)}
    bases = dict(parse_assignment(value, "--base") for value in args.base)
    pr_bases = dict(parse_assignment(value, "--pr-base") for value in args.pr_base)
    if len(repos) != len(args.repo):
        fail("duplicate repo names are not allowed")
    missing_bases = sorted(set(repos) - set(bases))
    extra_bases = sorted(set(bases) - set(repos))
    if missing_bases:
        fail("missing --base for repo(s): " + ", ".join(missing_bases))
    if extra_bases:
        fail("--base provided for unknown repo(s): " + ", ".join(extra_bases))
    if args.publish_if_clean:
        require_tool("gh")
        missing_pr_bases = sorted(set(repos) - set(pr_bases))
        extra_pr_bases = sorted(set(pr_bases) - set(repos))
        if missing_pr_bases:
            fail("--publish-if-clean requires --pr-base for repo(s): " + ", ".join(missing_pr_bases))
        if extra_pr_bases:
            fail("--pr-base provided for unknown repo(s): " + ", ".join(extra_pr_bases))
    elif args.pr_base:
        fail("--pr-base is only valid with --publish-if-clean")
    if args.plan:
        args.plan = args.plan.resolve()
        if not args.plan.is_file():
            fail(f"plan not found: {args.plan}")

    reviews = []
    for repo_name, repo_root in repos.items():
        if not repo_root.is_dir():
            fail(f"review repo root for {repo_name} not found: {repo_root}")
        require_linked_worktree(repo_name, repo_root)
        reviews.append(run_review(args, repo_name, repo_root, bases[repo_name]))

    publish_results: list[dict[str, object]] = []
    review_failed = any(review["status"] == "fail" for review in reviews)
    if args.publish_if_clean and not review_failed:
        if args.state_file:
            update_phase(args.state_file, "publish")
        reviews_by_repo = {str(review["repo"]): review for review in reviews}
        for repo_name, repo_root in repos.items():
            try:
                publish_results.append(
                    publish_repo_pr(
                        args,
                        repo_name,
                        repo_root,
                        pr_bases[repo_name],
                        reviews_by_repo[repo_name],
                        len(repos) > 1,
                    )
                )
            except SystemExit as exc:
                message = str(exc)
                if args.state_file:
                    update_phase(
                        args.state_file,
                        "blocked",
                        blocker={"phase": "publish", "repo": repo_name, "reason": "publish_failed", "message": message},
                        review={"status": "pass", "results": reviews},
                        publish={"status": "failed", "pull_requests": publish_results},
                    )
                    append_event(args.state_file, {"phase": "publish", "repo": repo_name, "status": "failed", "message": message})
                raise

    print(json.dumps({"reviews": reviews, "pull_requests": publish_results}, indent=2))
    if args.state_file:
        update_phase(
            args.state_file,
            "blocked" if review_failed else "complete",
            review={"status": "fail" if review_failed else "pass", "results": reviews},
            publish={"status": "skipped" if not args.publish_if_clean else ("skipped_review_failed" if review_failed else "complete"), "pull_requests": publish_results},
        )
    if review_failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

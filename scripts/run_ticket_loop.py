#!/usr/bin/env python3
import argparse
import atexit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import selectors
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from shutil import which

from ship_loop_state import (
    append_event,
    default_state_file,
    emit_state_path,
    logs_root,
    now_iso,
    read_state,
    update_phase,
    update_ticket,
    write_state,
)
from ship_loop_config import (
    DEFAULT_CODEX_BIN,
    ShipLoopConfigError,
    load_ship_loop_config,
)


CONFIDENCE_GATE = 84.7
CONFIDENCE_BREAKDOWN_TOLERANCE = 0.5
MAX_AUDIT_RUNS = 3
MAX_REPAIR_ATTEMPTS = MAX_AUDIT_RUNS - 1
ACTIVE_TICKET_STATUSES = {"implementing", "auditing", "repairing", "audit_repairing"}
RESUMABLE_PHASES = {"blocked", "failed", "paused"}
RESUMABLE_TICKET_STATUSES = {"blocked", "failed", "paused"}
QUOTA_PATTERNS = [
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"retry after", re.IGNORECASE),
    re.compile(r"try again after", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"\b429\b", re.IGNORECASE),
    re.compile(r"rate_limit_exceeded", re.IGNORECASE),
    re.compile(r"quota (?:exceeded|exhausted)", re.IGNORECASE),
    re.compile(r"exceeded your current quota", re.IGNORECASE),
    re.compile(r"insufficient quota", re.IGNORECASE),
]
APPROVAL_PROMPT_PATTERNS = [
    re.compile(r"\bapproval required\b", re.IGNORECASE),
    re.compile(r"\brequires approval\b", re.IGNORECASE),
    re.compile(r"\bapprove this command\b", re.IGNORECASE),
]
RUNTIME_BANNER_KEYS = {
    "workdir",
    "model",
    "approval",
    "sandbox",
    "reasoning effort",
}
PROOF_COMMAND_PREFIXES = (
    "bun ",
    "npm ",
    "pnpm ",
    "yarn ",
    "node ",
    "npx ",
    "vitest ",
    "jest ",
    "pytest ",
    "python ",
    "python3 ",
    "go ",
    "cargo ",
    "ruff ",
    "tsc ",
    "git ",
)
TICKET_HEADING = re.compile(r"^### Ticket ([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+) - (.+?)\s*$", re.MULTILINE)
STATUS_SERVER_METADATA = "status-server.json"
STATUS_SERVER_REGISTRY = "status-servers.jsonl"


class StaleChildError(RuntimeError):
    def __init__(self, message: str, log_path: Path, pid: int) -> None:
        super().__init__(message)
        self.log_path = log_path
        self.pid = pid


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "created_at": now_iso()}, sort_keys=True) + "\n"
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                fail(f"ship-loop lock exists and cannot be read: {self.path}")
            pid = existing.get("pid")
            if isinstance(pid, int) and not process_alive(pid):
                self.path.unlink()
                return self.acquire()
            fail(f"ship-loop helper is already running for this state file: {self.path}")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.acquired = False


ACTIVE_STATE_FILE_FOR_EXIT: Path | None = None
ACTIVE_STATUS_SERVER_FOR_EXIT: ThreadingHTTPServer | None = None
ACTIVE_RUN_LOCK_FOR_EXIT: RunLock | None = None


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def run(command: list[str], *, cwd: Path | None = None, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, input=stdin, text=True, capture_output=True, check=False)


def require_success(result: subprocess.CompletedProcess[str], description: str) -> None:
    if result.returncode == 0:
        return
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    fail(f"{description} failed with exit code {result.returncode}")


def process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_group(pid: int, grace_seconds: float = 5.0) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        os.kill(pid, signal.SIGKILL)


def parse_ps_time(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    days = 0
    if "-" in value:
        day_text, value = value.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return None
    parts = value.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = (int(part) for part in parts)
        elif len(parts) == 2:
            hours = 0
            minutes, seconds = (int(part) for part in parts)
        else:
            return None
    except ValueError:
        return None
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def child_cpu_seconds(pid: int | None) -> float | None:
    if pid is None:
        return None
    try:
        result = run(["ps", "-o", "time=", "-p", str(pid)])
    except PermissionError:
        return None
    if result.returncode != 0:
        return None
    return parse_ps_time(result.stdout)


def resolve_codex_bin(value: str) -> str:
    path = Path(value).expanduser()
    if path.is_absolute() or "/" in value:
        resolved = path.resolve()
        if not resolved.is_file():
            fail(f"codex binary not found: {resolved}")
        return str(resolved)
    located = which(value)
    if not located:
        fail(f"codex binary not found on PATH: {value}")
    return located


def default_codex_bin() -> str:
    return DEFAULT_CODEX_BIN


def apply_context_config(args: argparse.Namespace) -> None:
    try:
        config = load_ship_loop_config(args.workspace_root)
    except ShipLoopConfigError as exc:
        fail(str(exc))
    args.ship_loop_config = config
    args.codex_bin = args.codex_bin or config.codex_bin or default_codex_bin()
    args.model = args.model or config.model
    args.reasoning_effort = args.reasoning_effort or config.reasoning_effort
    if args.open_browser is None:
        args.open_browser = config.status_open_browser


def assert_no_quarantine(path: str, state_file: Path | None = None) -> None:
    if sys.platform != "darwin":
        return
    result = run(["xattr", "-p", "com.apple.quarantine", path])
    if result.returncode == 0:
        message = f"codex binary is quarantined by macOS: {path}"
        if state_file is not None and state_file.is_file():
            update_phase(
                state_file,
                "blocked",
                blocker={"phase": "preflight", "reason": "macos_quarantine", "codex_bin": path},
            )
        fail(message)


def parse_runtime_banner(text: str) -> dict[str, str]:
    banner: dict[str, str] = {}
    for line in text.splitlines()[:80]:
        stripped = line.strip()
        if ":" not in stripped:
            if stripped.startswith("{"):
                merge_runtime_json(banner, stripped)
            continue
        if stripped.startswith("{"):
            merge_runtime_json(banner, stripped)
            continue
        key, value = stripped.split(":", 1)
        normalized_key = key.strip().lower()
        if normalized_key in RUNTIME_BANNER_KEYS:
            banner[normalized_key] = value.strip()
    return banner


def merge_runtime_json(banner: dict[str, str], line: str) -> None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for raw_key, raw_value in value.items():
                key = str(raw_key).lower().replace("_", " ")
                if raw_value is not None:
                    text_value = str(raw_value)
                    if key in {"workdir", "cwd"}:
                        banner.setdefault("workdir", text_value)
                    elif key == "model":
                        banner.setdefault("model", text_value)
                    elif key in {"approval", "approval policy"}:
                        banner.setdefault("approval", text_value)
                    elif key in {"sandbox", "sandbox policy", "sandbox mode"}:
                        banner.setdefault("sandbox", text_value)
                    elif key in {"reasoning effort", "model reasoning effort"}:
                        banner.setdefault("reasoning effort", text_value)
                walk(raw_value)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)


def validate_runtime_banner(
    log_path: Path,
    *,
    expected_workdir: Path,
    expected_model: str,
    expected_reasoning_effort: str,
    expected_sandbox: str,
    expected_approval: str,
) -> list[str]:
    if not log_path.is_file():
        return ["runtime log is missing"]
    text = log_path.read_text(encoding="utf-8", errors="replace")
    banner = parse_runtime_banner(text)
    mismatches: list[str] = []
    expected = {
        "workdir": str(expected_workdir),
        "model": expected_model,
        "approval": expected_approval,
        "reasoning effort": expected_reasoning_effort,
    }
    for key, expected_value in expected.items():
        actual = banner.get(key)
        if actual is not None and actual != expected_value:
            mismatches.append(f"runtime {key} mismatch: expected {expected_value!r}, got {actual!r}")
    sandbox = banner.get("sandbox")
    if sandbox is not None and not sandbox.startswith(expected_sandbox):
        mismatches.append(f"runtime sandbox mismatch: expected prefix {expected_sandbox!r}, got {sandbox!r}")
    return mismatches


def missing_runtime_banner_keys(log_path: Path) -> list[str]:
    if not log_path.is_file():
        return []
    text = log_path.read_text(encoding="utf-8", errors="replace")
    banner = parse_runtime_banner(text)
    return sorted(RUNTIME_BANNER_KEYS - set(banner))


def require_runtime_banner(
    state_file: Path,
    log_path: Path,
    ticket: dict[str, str] | None,
    stage: str,
    attempt: int | None,
    target_root: Path,
    args: argparse.Namespace,
    sandbox: str,
    pid: int | None = None,
) -> None:
    expected_approval = args.approval_policy
    if expected_approval == "on-request":
        # Codex can normalize non-interactive child sessions to `never` in
        # the runtime banner. Actual approval prompts are still detected
        # separately and remain hard blockers.
        banner = parse_runtime_banner(log_path.read_text(encoding="utf-8", errors="replace")) if log_path.is_file() else {}
        if banner.get("approval") == "never":
            expected_approval = "never"
    mismatches = validate_runtime_banner(
        log_path,
        expected_workdir=target_root,
        expected_model=args.model,
        expected_reasoning_effort=args.reasoning_effort,
        expected_sandbox=sandbox,
        expected_approval=expected_approval,
    )
    if not mismatches:
        missing = missing_runtime_banner_keys(log_path)
        if missing:
            event = {
                "phase": "ticket_loop" if ticket is not None else stage,
                "stage": stage,
                "status": "runtime_metadata_unavailable",
                "log": str(log_path),
                "missing_keys": missing,
                "message": (
                    "Codex JSON event output did not include runtime banner metadata; "
                    "continuing after validating the exact helper command and schema output"
                ),
            }
            if ticket is not None:
                event["ticket_id"] = ticket["id"]
                event["target_repo"] = ticket["target_repo"]
                event["attempt"] = attempt or 0
            append_event(state_file, event)
        return
    if pid is not None and process_alive(pid):
        terminate_process_group(pid)
    message = "effective Codex runtime mismatch: " + "; ".join(mismatches)
    blocker = {"stage": stage, "reason": "runtime_mismatch", "log": str(log_path), "mismatches": mismatches}
    if ticket is not None:
        update_ticket(state_file, ticket["id"], status="blocked", stage=stage, attempt=attempt, log=str(log_path), last_error=message)
        blocker["ticket_id"] = ticket["id"]
        append_event(
            state_file,
            {
                "phase": "ticket_loop",
                "ticket_id": ticket["id"],
                "target_repo": ticket["target_repo"],
                "stage": stage,
                "status": "runtime_mismatch",
                "attempt": attempt or 0,
                "log": str(log_path),
                "message": message,
            },
        )
    else:
        append_event(state_file, {"phase": stage, "status": "runtime_mismatch", "log": str(log_path), "message": message})
    update_phase(state_file, "blocked", blocker=blocker, active_child=None)
    fail(message)


def file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def file_mtime_iso(path: Path) -> str | None:
    mtime = file_mtime(path)
    if mtime is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))


def file_age_seconds(path: Path) -> int | None:
    mtime = file_mtime(path)
    if mtime is None:
        return None
    return max(0, int(time.time() - mtime))


def runtime_status_root(args: argparse.Namespace, target_roots: dict[str, Path] | None = None) -> Path:
    workspace_mode = False
    if target_roots is not None:
        workspace_mode = len(target_roots) > 1
    if args.workspace_root is not None and args.planning_repo_root is not None:
        workspace_mode = workspace_mode or args.workspace_root != args.planning_repo_root
    root = args.workspace_root if workspace_mode else args.planning_repo_root
    if root is None:
        root = Path.cwd()
    return root / ".ship-loop" / str(args.plan_slug)


def exclude_runtime_status_dir(root: Path, runtime_root: Path) -> None:
    result = run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"])
    if result.returncode != 0:
        return
    exclude_path = Path(result.stdout.strip()).resolve()
    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
        rel = runtime_root.relative_to(root).as_posix().rstrip("/") + "/"
    except (OSError, ValueError):
        return
    if rel in {line.strip() for line in existing.splitlines()}:
        return
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    with exclude_path.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(f"{rel}\n")


def status_payload(state_file: Path) -> dict[str, object]:
    state = read_state(state_file)
    events = read_events(state_file)
    last_event = events[-1] if events else None
    active_child = state.get("active_child")
    child_summary = None
    if isinstance(active_child, dict):
        pid = active_child.get("pid")
        log_path = Path(str(active_child.get("log"))) if active_child.get("log") else None
        result_path = Path(str(active_child.get("result_path"))) if active_child.get("result_path") else None
        child_summary = {
            **active_child,
            "pid_alive": process_alive(pid if isinstance(pid, int) else None),
            "cpu_seconds": child_cpu_seconds(pid if isinstance(pid, int) else None),
            "log_mtime": file_mtime_iso(log_path) if log_path else None,
            "log_mtime_age_seconds": file_age_seconds(log_path) if log_path else None,
            "result_exists": result_path.is_file() if result_path else False,
        }
    tickets = state.get("tickets", [])
    counts: dict[str, int] = {}
    for ticket in tickets if isinstance(tickets, list) else []:
        if isinstance(ticket, dict):
            status = str(ticket.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
    recent_log_path = recent_status_log_path(state, child_summary, last_event)
    return {
        "state_file": str(state_file),
        "state_file_display": display_path(state_file),
        "phase": state.get("phase"),
        "plan_slug": state.get("plan_slug"),
        "plan_path": state.get("plan_path"),
        "tickets_total": len(tickets) if isinstance(tickets, list) else 0,
        "tickets_by_status": counts,
        "current": state.get("current"),
        "blocker": state.get("blocker"),
        "active_child": child_summary,
        "tickets": tickets,
        "config": state.get("config"),
        "last_event": last_event,
        "recent_log": log_tail_payload(recent_log_path, line_count=100),
        "updated_at": state.get("updated_at"),
        "review": state.get("review"),
    }


def recent_status_log_path(
    state: dict[str, object],
    active_child: dict[str, object] | None,
    last_event: dict[str, object] | None,
) -> Path | None:
    for source in (
        active_child,
        last_event,
        state.get("blocker") if isinstance(state.get("blocker"), dict) else None,
    ):
        if isinstance(source, dict) and source.get("log"):
            return Path(str(source["log"]))
    return None


def log_tail_payload(path: Path | None, line_count: int = 100, max_bytes: int = 200_000) -> dict[str, object] | None:
    if path is None:
        return None
    payload: dict[str, object] = {"path": str(path), "display_path": display_path(path), "lines": [], "truncated": False}
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read()
    except OSError as exc:
        payload["error"] = str(exc)
        return payload
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    payload["truncated"] = start > 0 or len(lines) > line_count
    payload["lines"] = lines[-line_count:]
    return payload


def display_path(path: Path | str) -> str:
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def render_status_html(payload: dict[str, object]) -> str:
    return render_status_template(payload)


def status_page_template_path() -> Path:
    return Path(__file__).with_name("status_page_template.html")


def json_for_script(payload: dict[str, object]) -> str:
    return (
        json.dumps(payload, sort_keys=True)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def render_status_template(payload: dict[str, object]) -> str:
    template = status_page_template_path().read_text(encoding="utf-8")
    return template.replace("__SHIP_LOOP_PAYLOAD_JSON__", json_for_script(payload))


def render_status_html_legacy(payload: dict[str, object]) -> str:
    tickets = payload.get("tickets") if isinstance(payload.get("tickets"), list) else []
    rows = []
    for ticket in tickets:
        if not isinstance(ticket, dict):
            continue
        status = str(ticket.get("status") or "unknown")
        rows.append(
            "<tr>"
            f"<td>{html_escape(ticket.get('id'))}</td>"
            f"<td>{html_escape(ticket.get('target_repo'))}</td>"
            f"<td>{html_escape(ticket.get('title'))}</td>"
            f"<td>{status_badge(status)}</td>"
            f"<td>{html_escape(ticket.get('stage'))}</td>"
            f"<td><code>{html_escape(short_sha(ticket.get('commit')))}</code></td>"
            "</tr>"
        )
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    counts = payload.get("tickets_by_status") if isinstance(payload.get("tickets_by_status"), dict) else {}
    active_child = payload.get("active_child") if isinstance(payload.get("active_child"), dict) else None
    blocker = payload.get("blocker") if isinstance(payload.get("blocker"), dict) else None
    last_event = payload.get("last_event") if isinstance(payload.get("last_event"), dict) else None
    current_ticket = current.get("ticket_id") if isinstance(current, dict) else None
    current_stage = current.get("stage") if isinstance(current, dict) else None
    active_items = []
    if active_child:
        active_items = [
            ("Ticket", active_child.get("ticket_id")),
            ("Stage", active_child.get("stage")),
            ("PID", active_child.get("pid")),
            ("Alive", yes_no(active_child.get("pid_alive"))),
            ("CPU", format_seconds(active_child.get("cpu_seconds"))),
            ("Log age", format_seconds(active_child.get("log_mtime_age_seconds"))),
            ("Result", "present" if active_child.get("result_exists") else "pending"),
        ]
    blocker_items = []
    if blocker:
        blocker_items = [
            ("Reason", blocker.get("reason")),
            ("Ticket", blocker.get("ticket_id")),
            ("Stage", blocker.get("stage") or blocker.get("phase")),
            ("Message", blocker.get("message")),
            ("Command", blocker.get("command")),
            ("Log", blocker.get("log")),
        ]
    event_items = []
    if last_event:
        event_items = [
            ("Phase", last_event.get("phase")),
            ("Status", last_event.get("status")),
            ("Ticket", last_event.get("ticket_id")),
            ("Stage", last_event.get("stage")),
            ("Message", last_event.get("message")),
        ]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ship Loop - {html_escape(payload.get('plan_slug'))}</title>
<style>
:root {{ color-scheme: light; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; --gap: 8px; --border: #ddd; --row-border: var(--border); }}
body {{ margin: 0; background: #fff; color: #111; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 16px; }}
h1 {{ font-size: 20px; margin: 0 0 2px; letter-spacing: 0; }}
h2 {{ font-size: 13px; margin: 12px 0 6px; letter-spacing: 0; }}
.meta {{ color: #555; font-size: 12px; }}
.controls {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; align-items: center; }}
.button {{ border: 1px solid var(--border); border-radius: 0; background: #fff; color: #111; padding: 5px 8px; font: inherit; font-size: 12px; cursor: pointer; }}
.button:hover {{ background: #f6f6f6; }}
.control-status {{ color: #555; font-size: 12px; min-height: 14px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: var(--gap); margin: 0 0 var(--gap); }}
.panel {{ background: #fff; border: 1px solid var(--border); border-radius: 0; padding: 10px; }}
.panel h2 {{ margin: -10px -10px 10px; padding: 6px 8px; background: #fafafa; border-bottom: 1px solid var(--row-border); }}
.label {{ color: #555; font-size: 11px; margin-bottom: 4px; }}
.value {{ font-size: 16px; font-weight: 600; }}
.subvalue {{ color: #555; font-size: 12px; margin-top: 2px; overflow-wrap: anywhere; }}
.status-list {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }}
.badge {{ display: inline-flex; align-items: center; border-radius: 0; padding: 2px 5px; font-size: 12px; line-height: 1.2; background: #fff; color: #111; border: 1px solid var(--border); }}
.details {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: var(--gap); margin-bottom: var(--gap); }}
.empty {{ color: #777; font-size: 12px; }}
code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #fff; border: 1px solid var(--border); border-radius: 0; padding: 8px; font-size: 12px; margin: 0 0 8px; }}
.path-row {{ margin-top: 0; }}
.state-row {{ display: grid; grid-template-columns: max-content minmax(0, 1fr); align-items: stretch; margin: 0 0 var(--gap); border: 1px solid var(--border); }}
.state-label {{ background: #fafafa; border-right: 1px solid var(--row-border); font-weight: 600; padding: 6px 8px; font-size: 12px; }}
.state-row pre {{ margin: 0; padding: 6px 8px; border: 0; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid var(--border); }}
th, td {{ text-align: left; border-bottom: 1px solid var(--row-border); padding: 6px 8px; font-size: 12px; vertical-align: top; }}
th {{ font-weight: 600; background: #fafafa; }}
tr:last-child td {{ border-bottom: 0; }}
.tickets-table {{ margin-top: 0; }}
.detail-table {{ border: 0; margin: -6px -8px; width: calc(100% + 16px); }}
.detail-table th {{ width: 72px; color: #555; background: #fff; font-weight: 400; }}
.detail-table th, .detail-table td {{ font-size: 12px; border-bottom: 1px solid var(--row-border); }}
.detail-table tr:last-child th, .detail-table tr:last-child td {{ border-bottom: 0; }}
</style>
<script>
function shipLoopEscape(value) {{
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}}

function shipLoopShortSha(value) {{
  return typeof value === "string" ? value.slice(0, 10) : "";
}}

function shipLoopYesNo(value) {{
  if (value === true) return "yes";
  if (value === false) return "no";
  return "";
}}

function shipLoopSeconds(value) {{
  if (typeof value !== "number") return "";
  if (value < 1) return "<1s";
  const total = Math.floor(value);
  if (total < 60) return total + "s";
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes < 60) return minutes + "m " + seconds + "s";
  return Math.floor(minutes / 60) + "h " + (minutes % 60) + "m";
}}

function shipLoopBadge(label) {{
  return '<span class="badge">' + shipLoopEscape(label) + '</span>';
}}

function shipLoopStatusCounts(counts) {{
  if (!counts || Object.keys(counts).length === 0) return '<span class="empty">None</span>';
  return Object.keys(counts).sort().map((status) => shipLoopBadge(status + ": " + counts[status])).join("");
}}

function shipLoopDetailRows(items, emptyText) {{
  const visible = items.filter((item) => item[1] !== null && item[1] !== undefined && item[1] !== "");
  if (visible.length === 0) return '<div class="empty">' + shipLoopEscape(emptyText) + '</div>';
  const rows = visible.map((item) =>
    '<tr><th>' + shipLoopEscape(item[0]) + '</th><td>' + shipLoopEscape(item[1]) + '</td></tr>'
  ).join("");
  return '<table class="detail-table"><tbody>' + rows + '</tbody></table>';
}}

function shipLoopRenderDetail(id, items, emptyText) {{
  const target = document.getElementById(id);
  if (target) target.innerHTML = shipLoopDetailRows(items, emptyText);
}}

function shipLoopRender(payload) {{
  document.title = "Ship Loop - " + shipLoopEscape(payload.plan_slug);
  const title = document.getElementById("plan-slug");
  if (title) title.textContent = payload.plan_slug || "";

  const ticketsTotal = document.getElementById("tickets-total");
  if (ticketsTotal) ticketsTotal.textContent = payload.tickets_total ?? 0;
  const statusCounts = document.getElementById("status-counts");
  if (statusCounts) statusCounts.innerHTML = shipLoopStatusCounts(payload.tickets_by_status || {{}});

  const current = payload.current && typeof payload.current === "object" ? payload.current : {{}};
  const currentTicket = document.getElementById("current-ticket");
  if (currentTicket) currentTicket.textContent = current.ticket_id || "None";
  const currentStage = document.getElementById("current-stage");
  if (currentStage) currentStage.textContent = current.stage || "";

  const stateFile = document.getElementById("state-file");
  if (stateFile) stateFile.textContent = payload.state_file || "";

  const active = payload.active_child && typeof payload.active_child === "object" ? payload.active_child : null;
  shipLoopRenderDetail("active-child-body", active ? [
    ["Ticket", active.ticket_id],
    ["Stage", active.stage],
    ["PID", active.pid],
    ["Alive", shipLoopYesNo(active.pid_alive)],
    ["CPU", shipLoopSeconds(active.cpu_seconds)],
    ["Log age", shipLoopSeconds(active.log_mtime_age_seconds)],
    ["Result", active.result_exists ? "present" : "pending"],
  ] : [], "No active child process");

  const event = payload.last_event && typeof payload.last_event === "object" ? payload.last_event : null;
  shipLoopRenderDetail("last-event-body", event ? [
    ["Phase", event.phase],
    ["Status", event.status],
    ["Ticket", event.ticket_id],
    ["Stage", event.stage],
    ["Message", event.message],
  ] : [], "No events recorded");

  const blocker = payload.blocker && typeof payload.blocker === "object" ? payload.blocker : null;
  shipLoopRenderDetail("blocker-body", blocker ? [
    ["Reason", blocker.reason],
    ["Ticket", blocker.ticket_id],
    ["Stage", blocker.stage || blocker.phase],
    ["Message", blocker.message],
    ["Command", blocker.command],
    ["Log", blocker.log],
  ] : [], "No blocker recorded");

  const ticketBody = document.getElementById("tickets-body");
  if (ticketBody) {{
    const tickets = Array.isArray(payload.tickets) ? payload.tickets : [];
    ticketBody.innerHTML = tickets.map((ticket) =>
      "<tr>" +
      "<td>" + shipLoopEscape(ticket.id) + "</td>" +
      "<td>" + shipLoopEscape(ticket.target_repo) + "</td>" +
      "<td>" + shipLoopEscape(ticket.title) + "</td>" +
      "<td>" + shipLoopBadge(ticket.status || "unknown") + "</td>" +
      "<td>" + shipLoopEscape(ticket.stage) + "</td>" +
      "<td><code>" + shipLoopEscape(shipLoopShortSha(ticket.commit)) + "</code></td>" +
      "</tr>"
    ).join("");
  }}
}}

async function shipLoopPoll() {{
  try {{
    const response = await fetch("/state.json", {{ cache: "no-store" }});
    if (!response.ok) throw new Error("status " + response.status);
    shipLoopRender(await response.json());
  }} catch (error) {{
    const status = document.getElementById("control-status");
    if (status) status.textContent = "Polling failed: " + String(error);
  }}
}}

async function shipLoopControl(path) {{
  const status = document.getElementById("control-status");
  status.textContent = "Working...";
  try {{
    const response = await fetch(path, {{ method: "POST" }});
    const text = await response.text();
    status.textContent = text;
    if (path === "/control/close" && response.ok) {{
      window.setTimeout(() => {{
        document.body.innerHTML = "<main><h1>Ship Loop</h1><div class='meta'>Status page closed.</div></main>";
      }}, 700);
    }} else {{
      window.setTimeout(shipLoopPoll, 900);
    }}
  }} catch (error) {{
    status.textContent = String(error);
  }}
}}

window.addEventListener("DOMContentLoaded", () => {{
  shipLoopPoll();
  window.setInterval(shipLoopPoll, 1000);
}});
</script>
</head>
<body>
<main>
<h1 id="plan-slug">{html_escape(payload.get('plan_slug'))}</h1>
<div class="controls">
  <button class="button" type="button" onclick="shipLoopControl('/control/pause')">Pause</button>
  <button class="button primary" type="button" onclick="shipLoopControl('/control/resume')">Resume</button>
  <button class="button" type="button" onclick="shipLoopControl('/control/close')">Close Page</button>
  <span id="control-status" class="control-status"></span>
</div>
<div class="grid">
  <div class="panel"><div class="label">Tickets</div><div id="tickets-total" class="value">{payload.get('tickets_total')}</div><div id="status-counts" class="status-list">{status_counts_html(counts)}</div></div>
  <div class="panel"><div class="label">Current</div><div id="current-ticket" class="value">{html_escape(current_ticket or 'None')}</div><div id="current-stage" class="subvalue">{html_escape(current_stage)}</div></div>
</div>
<div class="state-row"><div class="state-label">State File</div><pre id="state-file" class="path-row">{html_escape(payload.get('state_file'))}</pre></div>
<div class="details">
{detail_panel('Active Child', active_items, 'No active child process', 'active-child-body')}
{detail_panel('Last Event', event_items, 'No events recorded', 'last-event-body')}
{detail_panel('Blocker', blocker_items, 'No blocker recorded', 'blocker-body')}
</div>
<table class="tickets-table">
<thead><tr><th>Ticket</th><th>Repo</th><th>Title</th><th>Status</th><th>Stage</th><th>Commit</th></tr></thead>
<tbody id="tickets-body">{''.join(rows)}</tbody>
</table>
</main>
</body>
</html>
"""


def status_counts_html(counts: dict[str, object]) -> str:
    if not counts:
        return '<span class="empty">None</span>'
    items = []
    for status, count in sorted(counts.items()):
        items.append(f'{status_badge(str(status), label=f"{status}: {count}")}')
    return "".join(items)


def status_badge(status: str, label: str | None = None) -> str:
    return f'<span class="badge">{html_escape(label or status)}</span>'


def detail_panel(title: str, items: list[tuple[str, object]], empty_text: str, body_id: str) -> str:
    visible = [(key, value) for key, value in items if value is not None and value != ""]
    if not visible:
        body = f'<div class="empty">{html_escape(empty_text)}</div>'
    else:
        rows = "".join(
            f'<tr><th>{html_escape(key)}</th><td>{html_escape(value)}</td></tr>'
            for key, value in visible
        )
        body = f'<table class="detail-table"><tbody>{rows}</tbody></table>'
    return f'<section class="panel"><h2>{html_escape(title)}</h2><div id="{html_escape(body_id)}">{body}</div></section>'


def yes_no(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return ""


def format_seconds(value: object) -> str:
    if not isinstance(value, (int, float)):
        return ""
    if value < 1:
        return "<1s"
    seconds = int(value)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def html_escape(value: object) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def short_sha(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value[:10]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def status_server_records(runtime_root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    latest_path = runtime_root / STATUS_SERVER_METADATA
    if latest_path.is_file():
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            latest = None
        if isinstance(latest, dict):
            records.append(latest)

    registry_path = runtime_root / STATUS_SERVER_REGISTRY
    if registry_path.is_file():
        try:
            lines = registry_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def write_status_server_record(runtime_root: Path, record: dict[str, object]) -> None:
    runtime_root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    (runtime_root / STATUS_SERVER_METADATA).write_text(text, encoding="utf-8")
    with (runtime_root / STATUS_SERVER_REGISTRY).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def process_command(pid: int) -> str | None:
    result = run(["ps", "-p", str(pid), "-o", "command="])
    if result.returncode != 0:
        return None
    command = result.stdout.strip()
    return command or None


def wait_for_process_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.1)
    return not process_alive(pid)


def terminate_single_process(pid: int, grace_seconds: float = 2.0) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    if wait_for_process_exit(pid, grace_seconds):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return wait_for_process_exit(pid, 1.0)


def verified_status_server_process(pid: int, state_file: Path) -> bool:
    command = process_command(pid)
    if command is None:
        return False
    return (
        "run_ticket_loop.py" in command
        and "--serve-status" in command
        and str(state_file) in command
    )


def post_status_close(url: str) -> bool:
    target = url.rstrip("/") + "/control/close"
    request = urllib.request.Request(target, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False


def retire_existing_status_servers(
    state_file: Path,
    runtime_root: Path,
    *,
    current_generation: str,
    existing_records: list[dict[str, object]],
) -> None:
    seen: set[tuple[int | None, str | None]] = set()
    for record in [*existing_records, *status_server_records(runtime_root)]:
        if str(record.get("state_file") or "") != str(state_file):
            continue
        generation = str(record.get("generation")) if record.get("generation") else None
        pid = record.get("pid")
        key = (pid if isinstance(pid, int) else None, generation)
        if key in seen:
            continue
        seen.add(key)
        if generation == current_generation or pid == os.getpid():
            continue
        if not isinstance(pid, int) or not process_alive(pid):
            continue

        url = str(record.get("url") or "")
        closed = bool(url and post_status_close(url))
        if closed and wait_for_process_exit(pid, 2.0):
            append_event(
                state_file,
                {
                    "phase": "status_server",
                    "status": "retired_status_server",
                    "pid": pid,
                    "url": url,
                    "generation": generation,
                    "method": "close_endpoint",
                },
            )
            continue

        if verified_status_server_process(pid, state_file):
            terminated = terminate_single_process(pid)
            append_event(
                state_file,
                {
                    "phase": "status_server",
                    "status": "retired_status_server" if terminated else "retire_status_server_failed",
                    "pid": pid,
                    "url": url,
                    "generation": generation,
                    "method": "terminate_verified_process",
                },
            )
        else:
            append_event(
                state_file,
                {
                    "phase": "status_server",
                    "status": "retire_status_server_skipped",
                    "pid": pid,
                    "url": url,
                    "generation": generation,
                    "message": "process command did not verify as this ship-loop status server",
                },
            )


def status_server_superseded(status_server: ThreadingHTTPServer, state_file: Path) -> bool:
    generation = getattr(status_server, "ship_loop_generation", None)
    runtime_root = getattr(status_server, "ship_loop_runtime_root", None)
    if not isinstance(generation, str) or not isinstance(runtime_root, Path):
        return False
    latest_path = runtime_root / STATUS_SERVER_METADATA
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        str(latest.get("state_file") or "") == str(state_file)
        and latest.get("generation") not in {None, generation}
    )


def start_status_server(state_file: Path, runtime_root: Path, port: int, open_browser: bool) -> tuple[ThreadingHTTPServer, str]:
    runtime_root.mkdir(parents=True, exist_ok=True)
    existing_records = status_server_records(runtime_root)
    generation = f"{os.getpid()}-{time.time_ns()}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def send_json(self, status_code: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path not in {"/", "/state.json"}:
                self.send_error(404)
                return
            try:
                payload = status_payload(state_file)
                if self.path == "/state.json":
                    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                else:
                    body = render_status_html(payload).encode("utf-8")
                    content_type = "text/html; charset=utf-8"
            except Exception as exc:
                body = f"status unavailable: {exc}".encode("utf-8", errors="replace")
                content_type = "text/plain; charset=utf-8"
                self.send_response(500)
            else:
                self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path not in {"/control/pause", "/control/resume", "/control/close"}:
                self.send_error(404)
                return
            try:
                if self.path == "/control/pause":
                    payload = request_pause_from_status(state_file)
                elif self.path == "/control/resume":
                    payload = launch_resume_from_status(state_file, runtime_root)
                else:
                    payload = request_close_status_page(state_file)
            except SystemExit as exc:
                self.send_json(409, {"status": "rejected", "message": str(exc)})
            except Exception as exc:
                self.send_json(500, {"status": "error", "message": str(exc)})
            else:
                self.send_json(200, {"status": "ok", **payload})
                if self.path == "/control/close":
                    setattr(self.server, "ship_loop_close_requested", True)
                    threading.Thread(target=self.server.shutdown, daemon=True).start()

    if port > 0:
        retire_existing_status_servers(
            state_file,
            runtime_root,
            current_generation=generation,
            existing_records=existing_records,
        )
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    setattr(server, "ship_loop_generation", generation)
    setattr(server, "ship_loop_runtime_root", runtime_root)
    write_status_server_record(
        runtime_root,
        {
            "pid": os.getpid(),
            "url": url,
            "state_file": str(state_file),
            "generation": generation,
            "started_at": now_iso(),
        },
    )
    retire_existing_status_servers(
        state_file,
        runtime_root,
        current_generation=generation,
        existing_records=existing_records,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"SHIP_LOOP_STATUS_URL {url}", flush=True)
    if open_browser:
        marker = runtime_root / "opened_browser"
        if not marker.exists():
            try:
                webbrowser.open(url)
                marker.write_text(now_iso() + "\n", encoding="utf-8")
            except Exception as exc:
                append_event(state_file, {"phase": "status_server", "status": "open_browser_failed", "message": str(exc), "url": url})
    return server, url


def should_hold_status_server_after_exit(state_file: Path) -> bool:
    try:
        state = read_state(state_file)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    if state.get("phase") in {"blocked", "paused"} or state.get("blocker"):
        return True
    tickets = state.get("tickets")
    if not isinstance(tickets, list):
        return False
    return any(
        isinstance(ticket, dict) and ticket.get("status") in {"blocked", "failed", "paused"}
        for ticket in tickets
    )


def hold_status_server_after_blocker(
    state_file: Path,
    status_server: ThreadingHTTPServer | None,
    run_lock: RunLock | None,
    exit_message: str,
) -> None:
    if status_server is None or not should_hold_status_server_after_exit(state_file):
        return
    if run_lock is not None:
        run_lock.release()
    url = f"http://127.0.0.1:{status_server.server_port}/"
    append_event(
        state_file,
        {
            "phase": "status_server",
            "status": "holding_after_blocker",
            "url": url,
            "message": exit_message,
        },
    )
    print(f"SHIP_LOOP_STATUS_URL {url}", flush=True)
    try:
        while not getattr(status_server, "ship_loop_close_requested", False):
            if status_server_superseded(status_server, state_file):
                append_event(
                    state_file,
                    {
                        "phase": "status_server",
                        "status": "superseded_status_server",
                        "url": url,
                    },
                )
                break
            time.sleep(1)
    except KeyboardInterrupt:
        status_server.shutdown()
        status_server.server_close()
        raise
    status_server.shutdown()
    status_server.server_close()


def durable_result_path(state_file: Path, ticket_id: str, stage: str, attempt: int) -> Path:
    return logs_root(state_file) / ticket_id / f"{stage}-{attempt}.result.json"


def load_durable_result(path: Path, description: str) -> dict[str, object]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"{description} durable result is missing: {path}")
    except json.JSONDecodeError as exc:
        fail(f"{description} durable result is invalid JSON: {exc}; path: {path}")
    if not isinstance(parsed, dict):
        fail(f"{description} durable result must be a JSON object: {path}")
    return parsed


def archive_invalid_durable_result(state_file: Path, path: Path, reason: str, *, ticket: dict[str, str], stage: str, attempt: int) -> Path:
    if not path.is_file():
        fail(f"cannot archive invalid durable result because it is missing: {path}")
    archived = path.with_name(f"{path.stem}.invalid-{now_iso().replace(':', '').replace('.', '-')}{path.suffix}")
    path.replace(archived)
    append_event(
        state_file,
        {
            "phase": "ticket_loop",
            "ticket_id": ticket["id"],
            "target_repo": ticket["target_repo"],
            "stage": stage,
            "status": "invalid_adopted_result",
            "attempt": attempt,
            "result_path": str(path),
            "archived_result_path": str(archived),
            "message": reason,
        },
    )
    return archived


def record_validation_failure(
    state_file: Path,
    ticket: dict[str, str],
    *,
    stage: str,
    attempt: int,
    log_path: Path,
    message: str,
) -> None:
    update_ticket(
        state_file,
        ticket["id"],
        status="failed",
        stage=stage,
        attempt=attempt,
        log=str(log_path),
        last_error=message,
    )
    append_event(
        state_file,
        {
            "phase": "ticket_loop",
            "ticket_id": ticket["id"],
            "target_repo": ticket["target_repo"],
            "stage": stage,
            "status": "failed",
            "attempt": attempt,
            "log": str(log_path),
            "message": message,
        },
    )


def latest_existing_audit_repair_attempt(state_file: Path, ticket_id: str) -> int | None:
    latest: int | None = None
    for attempt in range(1, MAX_AUDIT_RUNS + 1):
        if durable_result_path(state_file, ticket_id, "audit_repair", attempt).is_file():
            latest = attempt
    return latest


def preflight_result_path(state_file: Path) -> Path:
    return logs_root(state_file) / "preflight.result.json"


def proof_log_path(state_file: Path, ticket_id: str, stage: str, attempt: int, index: int) -> Path:
    return logs_root(state_file) / ticket_id / f"proof-{stage}-{attempt}-{index}.log"


def split_backtick_commands(value: str) -> list[str]:
    commands: list[str] = []
    for match in re.finditer(r"`([^`]+)`", value):
        command = match.group(1).strip()
        if command and command.startswith(PROOF_COMMAND_PREFIXES):
            commands.append(command)
    return commands


def proof_commands(ticket: dict[str, str]) -> list[str]:
    commands = split_backtick_commands(ticket.get("required_verification", ""))
    seen: set[str] = set()
    unique = []
    for command in commands:
        if command not in seen:
            unique.append(command)
            seen.add(command)
    return unique


def failed_final_helper_proofs(
    tests: list[object],
    ticket: dict[str, str],
    expected_nonzero_probe=None,
) -> list[object]:
    helper_owned_proofs = set(proof_commands(ticket))
    latest_by_command: dict[str, object] = {}
    for test in tests:
        if not isinstance(test, dict):
            continue
        command = str(test.get("command") or "")
        if command in helper_owned_proofs:
            latest_by_command[command] = test

    failed: list[object] = []
    for test in latest_by_command.values():
        if not isinstance(test, dict):
            continue
        if test.get("exit_status") == 0:
            continue
        if expected_nonzero_probe is not None and expected_nonzero_probe(test):
            continue
        failed.append(test)
    return failed


def run_proof_commands(
    state_file: Path,
    ticket: dict[str, str],
    target_root: Path,
    stage: str,
    attempt: int,
    *,
    block_on_failure: bool = True,
) -> bool:
    commands = proof_commands(ticket)
    if not commands:
        message = f"{ticket['id']} has no concrete backtick proof command for helper-owned verification"
        update_ticket(state_file, ticket["id"], status="blocked", stage=stage, attempt=attempt, last_error=message)
        update_phase(state_file, "blocked", blocker={"ticket_id": ticket["id"], "stage": "verification", "reason": "missing_proof_command"})
        fail(message)
    all_passed = True
    for index, command in enumerate(commands, start=1):
        check_pause_requested(state_file, ticket_id=ticket["id"], stage="verification", attempt=attempt)
        log_path = proof_log_path(state_file, ticket["id"], stage, attempt, index)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        append_event(
            state_file,
            {
                "phase": "ticket_loop",
                "ticket_id": ticket["id"],
                "target_repo": ticket["target_repo"],
                "stage": "verification",
                "status": "started",
                "attempt": attempt,
                "command": command,
                "log": str(log_path),
            },
        )
        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(f"Started: {now_iso()}\n")
            log_file.write(f"Ticket: {ticket['id']}\n")
            log_file.write(f"Command: {command}\n\n")
            log_file.flush()
            result = subprocess.run(
                command,
                cwd=target_root,
                shell=True,
                text=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=False,
            )
            log_file.write(f"\nFinished: {now_iso()}\nExit code: {result.returncode}\n")
        check_pause_requested(state_file, ticket_id=ticket["id"], stage="verification", attempt=attempt, log=str(log_path))
        status = "complete" if result.returncode == 0 else "failed"
        append_event(
            state_file,
            {
                "phase": "ticket_loop",
                "ticket_id": ticket["id"],
                "target_repo": ticket["target_repo"],
                "stage": "verification",
                "status": status,
                "attempt": attempt,
                "command": command,
                "exit_status": result.returncode,
                "log": str(log_path),
            },
        )
        if result.returncode != 0:
            all_passed = False
            message = f"{ticket['id']} helper-owned proof command failed: {command}; log: {log_path}"
            if not block_on_failure:
                append_event(
                    state_file,
                    {
                        "phase": "ticket_loop",
                        "ticket_id": ticket["id"],
                        "target_repo": ticket["target_repo"],
                        "stage": "verification",
                        "status": "proof_failed_repairable",
                        "attempt": attempt,
                        "command": command,
                        "log": str(log_path),
                        "message": message,
                    },
                )
                continue
            update_ticket(state_file, ticket["id"], status="failed", stage=stage, attempt=attempt, log=str(log_path), last_error=message)
            update_phase(
                state_file,
                "blocked",
                blocker={"ticket_id": ticket["id"], "stage": "verification", "reason": "proof_failed", "command": command, "log": str(log_path)},
            )
            fail(message)
    return all_passed


def ticket_body(plan_text: str, ticket_id: str) -> str:
    matches = list(TICKET_HEADING.finditer(plan_text))
    for index, match in enumerate(matches):
        if match.group(1) != ticket_id:
            continue
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(plan_text)
        return plan_text[start:end].strip()
    fail(f"ticket body not found in plan for {ticket_id}")


def write_ticket_packet(state_file: Path, plan: Path, ticket: dict[str, str], target_root: Path) -> Path:
    plan_text = plan.read_text(encoding="utf-8")
    path = logs_root(state_file) / ticket["id"] / "ticket-packet.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    packet = f"""# Ship Loop Ticket Packet: {ticket['id']}

Plan: {plan}
Target repo: {ticket['target_repo']}
Target root: {target_root}

## Ticket Metadata

- Title: {ticket['title']}
- Dependencies: {', '.join(ticket['dependencies']) if ticket.get('dependencies') else 'None'}
- Expected files/modules: {ticket.get('expected_files_modules', '')}
- Required verification: {ticket.get('required_verification', '')}
- Helper-owned proof commands: {', '.join(proof_commands(ticket)) if proof_commands(ticket) else 'None'}

## Scoped Ticket Body

{ticket_body(plan_text, ticket['id'])}
"""
    path.write_text(packet, encoding="utf-8")
    return path


def active_ticket(state: dict[str, object]) -> dict[str, object] | None:
    current = state.get("current")
    if not isinstance(current, dict):
        return None
    ticket_id = current.get("ticket_id")
    for ticket in state.get("tickets", []):
        if isinstance(ticket, dict) and ticket.get("id") == ticket_id:
            return ticket
    return None


def set_active_child(state_file: Path, child: dict[str, object] | None) -> None:
    state = read_state(state_file)
    if child is None:
        state.pop("active_child", None)
    else:
        state["active_child"] = child
    write_state(state_file, state)


def clear_blocker(state_file: Path) -> None:
    state = read_state(state_file)
    state.pop("blocker", None)
    write_state(state_file, state)


def parse_target(value: str) -> tuple[str, Path]:
    if "=" not in value:
        fail(f"invalid --target-repo value {value!r}; expected NAME=PATH")
    name, path = value.split("=", 1)
    if not name or not path:
        fail(f"invalid --target-repo value {value!r}; expected NAME=PATH")
    return name, Path(path).resolve()


def git_status(repo: Path) -> str:
    result = run(["git", "-C", str(repo), "status", "--short"])
    require_success(result, f"git status in {repo}")
    return result.stdout


def require_linked_worktree(name: str, root: Path) -> None:
    git_file = root / ".git"
    if not git_file.is_file():
        fail(f"target repo {name} must be a linked worktree with a .git file, not the original repo root: {root}")
    result = run(["git", "-C", str(root), "rev-parse", "--show-toplevel"])
    require_success(result, f"git rev-parse in {root}")
    top_level = Path(result.stdout.strip()).resolve()
    if top_level != root:
        fail(f"target repo {name} top-level mismatch: expected {root}, got {top_level}")


def require_clean(repos: dict[str, Path]) -> None:
    dirty = []
    for name, repo in repos.items():
        status = git_status(repo)
        if status.strip():
            dirty.append(f"{name} ({repo})\n{status}")
    if dirty:
        fail("target repo worktrees must be clean before the ticket loop starts:\n" + "\n".join(dirty))


def git_value(repo: Path, args: list[str], description: str) -> str:
    result = run(["git", "-C", str(repo), *args])
    require_success(result, f"{description} in {repo}")
    return result.stdout.strip()


def repo_state(repo: Path) -> dict[str, object]:
    branch = git_value(repo, ["branch", "--show-current"], "git branch")
    return {
        "worktree": str(repo),
        "head": git_value(repo, ["rev-parse", "HEAD"], "git rev-parse HEAD"),
        "branch": branch or None,
        "dirty": bool(git_status(repo).strip()),
    }


def initialize_state(
    state_file: Path,
    args: argparse.Namespace,
    target_roots: dict[str, Path],
    tickets: list[dict[str, str]],
) -> None:
    created_at = now_iso()
    state = {
        "schema_version": 1,
        "plan_slug": args.plan_slug,
        "plan_path": str(args.plan),
        "mode": "workspace" if len(target_roots) > 1 or args.workspace_root != args.planning_repo_root else "regular",
        "phase": "initialized",
        "workspace_root": str(args.workspace_root),
        "planning_repo_root": str(args.planning_repo_root),
        "repos": {name: repo_state(root) for name, root in sorted(target_roots.items())},
        "config": args.ship_loop_config.state_payload(),
        "tickets": [
            {
                "id": ticket["id"],
                "target_repo": ticket["target_repo"],
                "title": ticket["title"],
                "status": "pending",
                "stage": None,
                "attempt": 0,
                "commit": None,
                "log": None,
                "last_error": None,
                "updated_at": created_at,
            }
            for ticket in tickets
        ],
        "current": None,
        "review": {"status": "pending", "findings": []},
        "followup_findings": [],
        "created_at": created_at,
        "updated_at": created_at,
    }
    write_state(state_file, state)


def update_repo_states(state_file: Path, target_roots: dict[str, Path]) -> None:
    state = read_state(state_file)
    state["repos"] = {name: repo_state(root) for name, root in sorted(target_roots.items())}
    write_state(state_file, state)


def active_ticket_from_state(state: dict[str, object]) -> dict[str, object] | None:
    current = state.get("current")
    if not isinstance(current, dict):
        return None
    ticket_id = current.get("ticket_id")
    tickets = state.get("tickets")
    if not isinstance(tickets, list):
        return None
    for ticket in tickets:
        if isinstance(ticket, dict) and ticket.get("id") == ticket_id:
            return ticket
    return None


def set_requested_action(state_file: Path, action: str | None) -> None:
    state = read_state(state_file)
    if action is None:
        state.pop("requested_action", None)
        state.pop("requested_action_at", None)
    else:
        state["requested_action"] = action
        state["requested_action_at"] = now_iso()
    write_state(state_file, state)


def pause_requested(state_file: Path) -> bool:
    try:
        state = read_state(state_file)
    except FileNotFoundError:
        return False
    return state.get("requested_action") == "pause"


def mark_paused(
    state_file: Path,
    *,
    ticket_id: str | None,
    stage: str | None,
    attempt: int | None = None,
    log: str | None = None,
    message: str = "ship-loop paused",
) -> None:
    state = read_state(state_file)
    if ticket_id is not None:
        for ticket in state.get("tickets", []):
            if isinstance(ticket, dict) and ticket.get("id") == ticket_id:
                ticket["status"] = "paused"
                if stage is not None:
                    ticket["stage"] = stage
                if attempt is not None:
                    ticket["attempt"] = attempt
                if log is not None:
                    ticket["log"] = log
                ticket["last_error"] = message
                ticket["updated_at"] = now_iso()
                state["current"] = {"ticket_id": ticket_id, "stage": stage}
                break
    state["phase"] = "paused"
    state["blocker"] = None
    state["active_child"] = None
    state.pop("requested_action", None)
    state.pop("requested_action_at", None)
    write_state(state_file, state)
    event = {
        "phase": "ticket_loop",
        "stage": stage,
        "status": "paused",
        "message": message,
    }
    if ticket_id is not None:
        event["ticket_id"] = ticket_id
    if attempt is not None:
        event["attempt"] = attempt
    if log is not None:
        event["log"] = log
    append_event(state_file, event)


def check_pause_requested(
    state_file: Path,
    *,
    ticket_id: str | None = None,
    stage: str | None = None,
    attempt: int | None = None,
    log: str | None = None,
) -> None:
    if not pause_requested(state_file):
        return
    mark_paused(
        state_file,
        ticket_id=ticket_id,
        stage=stage,
        attempt=attempt,
        log=log,
        message="ship-loop paused at a safe checkpoint",
    )
    fail("ship-loop paused")


def read_events(state_file: Path) -> list[dict[str, object]]:
    event_path = state_file.with_name("events.jsonl")
    events: list[dict[str, object]] = []
    if not event_path.is_file():
        return events
    for line in event_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"status": "invalid_json", "raw": line})
    return events


def state_consistency_warnings(
    state_file: Path,
    target_roots: dict[str, Path] | None = None,
    plan_abbrev: str | None = None,
) -> list[str]:
    state = read_state(state_file)
    warnings: list[str] = []
    phase = state.get("phase")
    blocker = state.get("blocker")
    if blocker and phase != "blocked":
        warnings.append("state has a blocker but phase is not blocked")
    current = state.get("current")
    current_ticket = active_ticket(state)
    if current and current_ticket is None:
        warnings.append(f"state current ticket is not present in ticket list: {current}")
    if current_ticket is not None:
        status = current_ticket.get("status")
        stage = current.get("stage") if isinstance(current, dict) else None
        if status in ACTIVE_TICKET_STATUSES and stage != current_ticket.get("stage"):
            warnings.append("current stage does not match active ticket stage")
    active_child = state.get("active_child")
    if isinstance(active_child, dict):
        pid = active_child.get("pid")
        if not isinstance(pid, int):
            warnings.append("active_child is missing integer pid")
        elif not process_alive(pid):
            warnings.append(f"active_child pid is not alive: {pid}")
        log_path = active_child.get("log")
        if isinstance(log_path, str) and not Path(log_path).is_file():
            warnings.append(f"active_child log path does not exist: {log_path}")
        result_path = active_child.get("result_path")
        if isinstance(result_path, str) and Path(result_path).exists() and Path(result_path).stat().st_size == 0:
            warnings.append(f"active_child result path is empty: {result_path}")
    for ticket in state.get("tickets", []):
        if not isinstance(ticket, dict):
            warnings.append("state contains a non-object ticket")
            continue
        log = ticket.get("log")
        if log and not Path(str(log)).is_file():
            warnings.append(f"{ticket.get('id')} log path does not exist: {log}")
        commit = ticket.get("commit")
        if ticket.get("status") == "committed" and not commit:
            warnings.append(f"{ticket.get('id')} is committed without a commit sha")
        if target_roots is not None and plan_abbrev is not None and commit:
            target_repo = ticket.get("target_repo")
            repo = target_roots.get(str(target_repo))
            if repo is not None and ticket_commit_sha(repo, plan_abbrev, str(ticket.get("id"))) != commit:
                warnings.append(f"{ticket.get('id')} commit does not match git log in {target_repo}")
    if target_roots is not None:
        dirty = {name: git_status(repo) for name, repo in target_roots.items() if git_status(repo).strip()}
        if len(dirty) > 1:
            warnings.append("more than one target repo is dirty")
        if dirty and current_ticket is not None:
            dirty_name = next(iter(dirty))
            if current_ticket.get("target_repo") != dirty_name:
                warnings.append(
                    f"dirty repo {dirty_name} does not match current ticket target {current_ticket.get('target_repo')}"
                )
    return warnings


def require_consistent_state(
    state_file: Path,
    target_roots: dict[str, Path] | None = None,
    plan_abbrev: str | None = None,
) -> None:
    warnings = state_consistency_warnings(state_file, target_roots, plan_abbrev)
    if warnings:
        fail("state consistency validation failed:\n" + "\n".join(f"- {warning}" for warning in warnings))


def show_status(state_file: Path) -> None:
    emit_state_path(state_file)
    state = read_state(state_file)
    state["_consistency_warnings"] = state_consistency_warnings(state_file)
    print(json.dumps(state, indent=2, sort_keys=True))


def show_summary(state_file: Path) -> None:
    state = read_state(state_file)
    events = read_events(state_file)
    stage_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    durations: dict[str, list[int]] = {}
    for event in events:
        key = str(event.get("stage") or event.get("phase") or "unknown")
        if event.get("status") == "started":
            stage_counts[key] = stage_counts.get(key, 0) + 1
        status = str(event.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if event.get("status") in {"complete", "pass", "failed", "blocked", "quota_blocked"}:
            elapsed = event.get("elapsed_seconds")
            if isinstance(elapsed, int):
                durations.setdefault(key, []).append(elapsed)
    tickets = state.get("tickets", [])
    active_child = state.get("active_child")
    child_summary = None
    if isinstance(active_child, dict):
        log_path = Path(str(active_child.get("log"))) if active_child.get("log") else None
        result_path = Path(str(active_child.get("result_path"))) if active_child.get("result_path") else None
        pid = active_child.get("pid")
        child_summary = {
            **active_child,
            "pid_alive": process_alive(pid if isinstance(pid, int) else None),
            "cpu_seconds": child_cpu_seconds(pid if isinstance(pid, int) else None),
            "log_mtime": file_mtime_iso(log_path) if log_path else None,
            "log_mtime_age_seconds": file_age_seconds(log_path) if log_path else None,
            "result_exists": result_path.is_file() if result_path else False,
        }
    last_event = events[-1] if events else None
    dirty_repos = {}
    for name, repo in (state.get("repos") or {}).items():
        if isinstance(repo, dict) and repo.get("dirty"):
            dirty_repos[name] = repo
    summary = {
        "state_file": str(state_file),
        "phase": state.get("phase"),
        "plan_slug": state.get("plan_slug"),
        "tickets_total": len(tickets),
        "tickets_by_status": {},
        "current": state.get("current"),
        "current_ticket": active_ticket(state),
        "blocker": state.get("blocker"),
        "active_child": child_summary,
        "last_event": last_event,
        "stage_start_counts": stage_counts,
        "event_status_counts": status_counts,
        "stage_duration_seconds": {
            stage: {
                "count": len(values),
                "max": max(values),
                "min": min(values),
                "average": round(sum(values) / len(values), 1),
            }
            for stage, values in sorted(durations.items())
            if values
        },
        "stall_events": [
            event
            for event in events
            if event.get("status") in {"possibly_stalled", "stale_child", "approval_prompt", "runtime_mismatch"}
        ][-20:],
        "quota_events": [
            event
            for event in events
            if event.get("status") == "quota_blocked" or (isinstance(event.get("message"), str) and "quota" in str(event.get("message")).lower())
        ][-20:],
        "followup_findings": state.get("followup_findings", []),
        "dirty_repos_from_state": dirty_repos,
        "consistency_warnings": state_consistency_warnings(state_file),
        "blocked_or_failed": [
            ticket
            for ticket in tickets
            if ticket.get("status") in {"blocked", "failed"}
        ],
    }
    for ticket in tickets:
        status = str(ticket.get("status"))
        summary["tickets_by_status"][status] = summary["tickets_by_status"].get(status, 0) + 1
    print(json.dumps(summary, indent=2, sort_keys=True))


def record_followup_findings(state_file: Path, ticket: dict[str, str], audit_result: dict[str, object]) -> None:
    followups = [
        finding
        for finding in audit_result.get("findings", [])
        if finding.get("scope") in {"adjacent_followup", "out_of_scope"}
    ]
    if not followups:
        return
    state = read_state(state_file)
    state.setdefault("followup_findings", [])
    existing = {
        json.dumps(
            {
                key: finding.get(key)
                for key in ("ticket_id", "target_repo", "scope", "file", "line", "description", "required_fix")
            },
            sort_keys=True,
        )
        for finding in state["followup_findings"]
    }
    added = 0
    for finding in followups:
        entry = {
            "ticket_id": ticket["id"],
            "target_repo": ticket["target_repo"],
            **finding,
        }
        key = json.dumps(
            {
                field: entry.get(field)
                for field in ("ticket_id", "target_repo", "scope", "file", "line", "description", "required_fix")
            },
            sort_keys=True,
        )
        if key in existing:
            continue
        existing.add(key)
        state["followup_findings"].append(entry)
        added += 1
    if added == 0:
        return
    write_state(state_file, state)
    append_event(
        state_file,
        {
            "phase": "ticket_loop",
            "ticket_id": ticket["id"],
            "target_repo": ticket["target_repo"],
            "stage": "audit",
            "status": "followup_findings",
            "count": added,
        },
    )


def first_uncommitted_ticket(
    tickets: list[dict[str, str]],
    target_roots: dict[str, Path],
    plan_abbrev: str,
) -> dict[str, str] | None:
    for ticket in tickets:
        repo = target_roots[ticket["target_repo"]]
        if not ticket_commit_exists(repo, plan_abbrev, ticket["id"]):
            return ticket
    return None


def reconcile_resume(
    state_file: Path,
    tickets: list[dict[str, str]],
    target_roots: dict[str, Path],
    plan_abbrev: str,
    adopt_dirty_ticket: str | None,
) -> None:
    state = read_state(state_file)
    expected_ids = [ticket["id"] for ticket in tickets]
    state_ids = [ticket["id"] for ticket in state.get("tickets", [])]
    if state_ids != expected_ids:
        fail(f"resume state ticket order does not match parsed plan: state={state_ids}, plan={expected_ids}")

    dirty_repos = {}
    for name, repo in target_roots.items():
        status = git_status(repo)
        if status.strip():
            dirty_repos[name] = status
    if len(dirty_repos) > 1:
        fail(
            "resume permits at most one dirty target repo; found:\n"
            + "\n".join(f"{name}\n{status}" for name, status in dirty_repos.items())
        )
    if dirty_repos:
        active = state.get("current") or {}
        active_ticket_id = active.get("ticket_id")
        active_ticket = next((ticket for ticket in tickets if ticket["id"] == active_ticket_id), None)
        blocker = state.get("blocker")
        if adopt_dirty_ticket is not None:
            active_ticket = next((ticket for ticket in tickets if ticket["id"] == adopt_dirty_ticket), None)
            if active_ticket is None:
                fail(f"--adopt-dirty-ticket names an unknown ticket: {adopt_dirty_ticket}")
        elif active_ticket is None or blocker:
            fail(
                "resume found dirty work but the state is blocked or does not name an active ticket; "
                "rerun with --adopt-dirty-ticket TICKET_ID after verifying the dirty work belongs to that ticket"
            )
        if active_ticket is None:
            fail("resume found dirty work but every parsed ticket already has a ticket commit")
        dirty_name = next(iter(dirty_repos))
        if active_ticket["target_repo"] != dirty_name:
            fail(
                "resume dirty worktree does not match the active ticket target repo: "
                f"dirty={dirty_name}, active={active_ticket['id']} target={active_ticket['target_repo']}"
            )


def extract_tickets(plan: Path, allowed_repos: list[str]) -> list[dict[str, str]]:
    script = Path(__file__).with_name("extract_tickets.py")
    command = [sys.executable, str(script), str(plan)]
    for repo in allowed_repos:
        command.extend(["--allowed-target-repo", repo])
    result = run(command)
    require_success(result, "ticket extraction")
    return json.loads(result.stdout)["tickets"]


def schema_path(name: str) -> Path:
    path = Path(__file__).parents[1] / "schemas" / name
    if not path.is_file():
        fail(f"schema file not found: {path}")
    return path


def render_ticket_prompt(template: Path, replacements: dict[str, str]) -> str:
    script = Path(__file__).with_name("render_prompt.py")
    command = [sys.executable, str(script), str(template)]
    for key, value in replacements.items():
        command.extend(["--set", f"{key}={value}"])
    result = run(command)
    require_success(result, "ticket prompt rendering")
    return result.stdout


def codex_exec_command(
    args: argparse.Namespace,
    target_root: Path,
    output_schema: Path,
    output_last_message: Path,
    sandbox: str,
) -> list[str]:
    command = [
        args.codex_bin,
        "-a",
        args.approval_policy,
        "exec",
        "-C",
        str(target_root),
        "-m",
        args.model,
        "-c",
        f'model_reasoning_effort="{args.reasoning_effort}"',
        "-s",
        sandbox,
        "--output-schema",
        str(output_schema),
        "--output-last-message",
        str(output_last_message),
    ]
    if args.codex_json_events:
        command.append("--json")
    for add_dir in args.add_dir:
        command.extend(["--add-dir", str(add_dir)])
    command.append("-")
    return command


def is_quota_blocker(log_path: Path) -> bool:
    if not log_path.is_file():
        return False
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    text = "\n".join(lines[-240:])
    return any(pattern.search(text) for pattern in QUOTA_PATTERNS)


def run_codex_preflight(args: argparse.Namespace, state_file: Path, target_root: Path) -> None:
    schema = logs_root(state_file) / "preflight.schema.json"
    output_last_message = preflight_result_path(state_file)
    log_path = logs_root(state_file) / "preflight.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    schema.write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {"status": {"type": "string", "enum": ["ok"]}},
            }
        ),
        encoding="utf-8",
    )
    output_last_message.unlink(missing_ok=True)
    command = codex_exec_command(args, target_root, schema, output_last_message, "read-only")
    append_event(
        state_file,
        {
            "phase": "preflight",
            "status": "started",
            "log": str(log_path),
            "result_path": str(output_last_message),
            "codex_bin": args.codex_bin,
        },
    )
    with log_path.open("w", encoding="utf-8") as log_file:
        result = subprocess.run(
            command,
            cwd=target_root,
            input='Return only {"status":"ok"}.\n',
            text=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=False,
        )
    require_runtime_banner(state_file, log_path, None, "preflight", None, target_root, args, "read-only")
    if result.returncode != 0:
        status = "quota_blocked" if is_quota_blocker(log_path) else "failed"
        update_phase(state_file, "blocked", blocker={"phase": "preflight", "status": status, "log": str(log_path)})
        append_event(state_file, {"phase": "preflight", "status": status, "log": str(log_path)})
        fail(f"Codex preflight failed with exit code {result.returncode}; log: {log_path}")
    try:
        parsed = json.loads(output_last_message.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        update_phase(state_file, "blocked", blocker={"phase": "preflight", "status": "invalid_result", "log": str(log_path)})
        fail(f"Codex preflight did not produce valid schema output: {exc}; log: {log_path}")
    if parsed.get("status") != "ok":
        fail(f"Codex preflight returned unexpected result: {parsed}; log: {log_path}")
    if read_state(state_file).get("blocker"):
        clear_blocker(state_file)
        update_phase(state_file, "ticket_loop")
    append_event(state_file, {"phase": "preflight", "status": "complete", "log": str(log_path), "result_path": str(output_last_message)})


def run_codex_agent(
    args: argparse.Namespace,
    state_file: Path,
    ticket: dict[str, str],
    target_root: Path,
    prompt: str,
    output_schema: Path,
    description: str,
    sandbox: str,
    stage: str,
    state_status: str,
    log_path: Path,
    attempt: int,
) -> dict[str, object]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    output_last_message = durable_result_path(state_file, ticket["id"], stage, attempt)
    if args.resume and output_last_message.is_file():
        parsed = load_durable_result(output_last_message, description)
        parsed["_ship_loop_adopted_result"] = True
        update_ticket(
            state_file,
            ticket["id"],
            status=state_status,
            stage=stage,
            attempt=attempt,
            log=str(log_path),
            last_error=None,
        )
        append_event(
            state_file,
            {
                "phase": "ticket_loop",
                "ticket_id": ticket["id"],
                "target_repo": ticket["target_repo"],
                "stage": stage,
                "status": "adopted_result",
                "attempt": attempt,
                "log": str(log_path),
                "result_path": str(output_last_message),
                "message": "resume adopted existing durable child result instead of rerunning the child agent",
            },
        )
        return parsed

    if read_state(state_file).get("blocker"):
        clear_blocker(state_file)
        update_phase(state_file, "ticket_loop")
    update_ticket(
        state_file,
        ticket["id"],
        status=state_status,
        stage=stage,
        attempt=attempt,
        log=str(log_path),
    )
    append_event(
        state_file,
        {
            "phase": "ticket_loop",
            "ticket_id": ticket["id"],
            "target_repo": ticket["target_repo"],
            "stage": stage,
            "status": "started",
            "attempt": attempt,
            "log": str(log_path),
            "result_path": str(durable_result_path(state_file, ticket["id"], stage, attempt)),
        },
    )
    output_last_message.parent.mkdir(parents=True, exist_ok=True)
    output_last_message.unlink(missing_ok=True)
    command = codex_exec_command(args, target_root, output_schema, output_last_message, sandbox)
    started_at = time.monotonic()
    structured_last: dict[str, object] | None = None
    current_tool: str | None = None
    banner_validated = False
    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write(f"Started: {now_iso()}\n")
        log_file.write(f"Description: {description}\n")
        log_file.write(f"Command: {shlex.join(command)}\n\n")
        log_file.flush()
        process = subprocess.Popen(
            command,
            cwd=target_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            bufsize=1,
        )
        set_active_child(
            state_file,
            {
                "pid": process.pid,
                "ticket_id": ticket["id"],
                "target_repo": ticket["target_repo"],
                "stage": stage,
                "attempt": attempt,
                "log": str(log_path),
                "result_path": str(output_last_message),
                "started_at": now_iso(),
            },
        )
        try:
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()
        except BrokenPipeError:
            pass

        selector = selectors.DefaultSelector()
        if process.stdout is not None:
            selector.register(process.stdout, selectors.EVENT_READ)
        next_heartbeat = started_at + args.heartbeat_seconds
        last_progress_at = started_at
        possible_stall_reported = False
        pause_termination_sent = False
        stdout_closed = False
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
                    require_runtime_banner(state_file, log_path, ticket, stage, attempt, target_root, args, sandbox, process.pid)
                    banner_validated = True
                if any(pattern.search(stripped) for pattern in APPROVAL_PROMPT_PATTERNS):
                    terminate_process_group(process.pid)
                    message = f"{description} reached an interactive approval prompt; log: {log_path}"
                    update_ticket(state_file, ticket["id"], status="blocked", stage=stage, attempt=attempt, log=str(log_path), last_error=message)
                    update_phase(
                        state_file,
                        "blocked",
                        blocker={"ticket_id": ticket["id"], "stage": stage, "reason": "approval_prompt", "log": str(log_path), "pid": process.pid},
                        active_child=None,
                    )
                    append_event(
                        state_file,
                        {
                            "phase": "ticket_loop",
                            "ticket_id": ticket["id"],
                            "target_repo": ticket["target_repo"],
                            "stage": stage,
                            "status": "approval_prompt",
                            "attempt": attempt,
                            "log": str(log_path),
                            "message": message,
                        },
                    )
                    fail(message)
                if stripped.startswith("{"):
                    try:
                        parsed_event = json.loads(stripped)
                    except json.JSONDecodeError:
                        parsed_event = None
                    if isinstance(parsed_event, dict):
                        structured_last = parsed_event
                        event_type = str(parsed_event.get("type") or parsed_event.get("event") or "")
                        payload = parsed_event.get("payload")
                        if "tool" in event_type or "function" in event_type:
                            if isinstance(payload, dict):
                                current_tool = str(payload.get("name") or payload.get("command") or event_type)
                            else:
                                current_tool = event_type

            now = time.monotonic()
            if pause_requested(state_file) and not pause_termination_sent:
                terminate_process_group(process.pid)
                append_event(
                    state_file,
                    {
                        "phase": "ticket_loop",
                        "ticket_id": ticket["id"],
                        "target_repo": ticket["target_repo"],
                        "stage": stage,
                        "status": "pause_requested",
                        "attempt": attempt,
                        "pid": process.pid,
                        "log": str(log_path),
                        "message": "active child process group terminated for pause",
                    },
                )
                pause_termination_sent = True
            result_exists = output_last_message.is_file()
            if result_exists:
                last_progress_at = now
            if args.heartbeat_seconds > 0 and now >= next_heartbeat:
                inactive_seconds = int(now - last_progress_at)
                heartbeat = {
                    "phase": "ticket_loop",
                    "ticket_id": ticket["id"],
                    "target_repo": ticket["target_repo"],
                    "stage": stage,
                    "status": "running",
                    "attempt": attempt,
                    "elapsed_seconds": int(now - started_at),
                    "inactive_seconds": inactive_seconds,
                    "pid": process.pid,
                    "cpu_seconds": child_cpu_seconds(process.pid),
                    "log": str(log_path),
                    "log_mtime": file_mtime_iso(log_path),
                    "log_mtime_age_seconds": file_age_seconds(log_path),
                    "result_path": str(output_last_message),
                    "result_exists": result_exists,
                    "current_tool": current_tool,
                    "last_structured_event_type": structured_last.get("type") if structured_last else None,
                }
                append_event(state_file, heartbeat)
                stale_warning_seconds = args.heartbeat_seconds * args.stale_warning_heartbeats
                stale_fail_seconds = args.heartbeat_seconds * args.stale_fail_heartbeats
                if (
                    args.stale_warning_heartbeats > 0
                    and inactive_seconds >= stale_warning_seconds
                    and not possible_stall_reported
                ):
                    append_event(state_file, {**heartbeat, "status": "possibly_stalled"})
                    possible_stall_reported = True
                if args.stale_fail_heartbeats > 0 and inactive_seconds >= stale_fail_seconds:
                    terminate_process_group(process.pid)
                    message = (
                        f"{description} appears stalled: no child structured/log/result progress for "
                        f"{inactive_seconds}s; log: {log_path}"
                    )
                    update_ticket(
                        state_file,
                        ticket["id"],
                        status="blocked",
                        stage=stage,
                        attempt=attempt,
                        log=str(log_path),
                        last_error=message,
                    )
                    update_phase(
                        state_file,
                        "blocked",
                        blocker={
                            "ticket_id": ticket["id"],
                            "stage": stage,
                            "reason": "stale_child",
                            "log": str(log_path),
                            "pid": process.pid,
                            "result_path": str(output_last_message),
                            "inactive_seconds": inactive_seconds,
                            "current_tool": current_tool,
                            "last_structured_event_type": structured_last.get("type") if structured_last else None,
                        },
                    )
                    append_event(state_file, {**heartbeat, "status": "stale_child", "message": message})
                    set_active_child(state_file, None)
                    fail(message)
                next_heartbeat = now + args.heartbeat_seconds
        returncode = process.returncode
        if pause_requested(state_file):
            log_file.write(f"\nPaused: {now_iso()}\nExit code: {returncode}\n")
            mark_paused(
                state_file,
                ticket_id=ticket["id"],
                stage=stage,
                attempt=attempt,
                log=str(log_path),
                message="ship-loop paused",
            )
            fail("ship-loop paused")
        if not banner_validated:
            require_runtime_banner(state_file, log_path, ticket, stage, attempt, target_root, args, sandbox, process.pid)
        log_file.write(f"\nFinished: {now_iso()}\nExit code: {returncode}\n")

    set_active_child(state_file, None)
    if returncode != 0:
        quota_blocked = is_quota_blocker(log_path)
        failure_status = "blocked" if quota_blocked else "failed"
        message = f"{description} failed with exit code {returncode}; log: {log_path}"
        if quota_blocked:
            message = f"{description} hit a Codex quota/rate-limit blocker; stop and resume later; log: {log_path}"
        update_ticket(state_file, ticket["id"], status=failure_status, stage=stage, attempt=attempt, log=str(log_path), last_error=message)
        if quota_blocked:
            update_phase(state_file, "blocked", blocker={"ticket_id": ticket["id"], "stage": stage, "reason": "quota", "log": str(log_path)})
        append_event(
            state_file,
            {
                "phase": "ticket_loop",
                "ticket_id": ticket["id"],
                "target_repo": ticket["target_repo"],
                "stage": stage,
                "status": "quota_blocked" if quota_blocked else "failed",
                "attempt": attempt,
                "log": str(log_path),
                "message": message,
            },
        )
        fail(message)
    if not output_last_message.is_file():
        message = f"{description} did not write an output-last-message file; log: {log_path}"
        update_ticket(state_file, ticket["id"], status="failed", stage=stage, attempt=attempt, log=str(log_path), last_error=message)
        fail(message)
    try:
        parsed = json.loads(output_last_message.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        message = f"{description} wrote invalid JSON: {exc}; log: {log_path}"
        update_ticket(state_file, ticket["id"], status="failed", stage=stage, attempt=attempt, log=str(log_path), last_error=message)
        fail(message)
    append_event(
        state_file,
        {
            "phase": "ticket_loop",
            "ticket_id": ticket["id"],
            "target_repo": ticket["target_repo"],
            "stage": stage,
            "status": "complete",
            "attempt": attempt,
            "elapsed_seconds": int(time.monotonic() - started_at),
            "log": str(log_path),
            "result_path": str(output_last_message),
        },
    )
    return parsed


def require_ticket_complete(result: dict[str, object], ticket: dict[str, str]) -> None:
    ticket_id = ticket["id"]
    if result.get("ticket_id") != ticket_id:
        fail(f"{ticket_id} result ticket_id mismatch: {result.get('ticket_id')!r}")
    if result.get("target_repo") != ticket["target_repo"]:
        fail(f"{ticket_id} result target_repo mismatch: {result.get('target_repo')!r}")
    if result.get("status") != "complete":
        fail(f"{ticket_id} did not complete; status was {result.get('status')!r}")

    confidence = float(result.get("confidence", -1))

    breakdown = result.get("confidence_breakdown")
    if not isinstance(breakdown, dict):
        fail(f"{ticket_id} result missing confidence_breakdown object")
    total = sum(float(breakdown[key]) for key in ("testing", "code_review", "logical_inspection"))

    # Some Codex CLI agents report the required 0-100 confidence gate as a
    # 0-1 fraction. Normalize fractional weighted totals and the unweighted
    # fractional category form: testing/code_review/logical_inspection each
    # scored 0-1 against the AGENTS 40/30/30 weights.
    if 0 <= confidence <= 1:
        testing = float(breakdown["testing"])
        code_review = float(breakdown["code_review"])
        logical_inspection = float(breakdown["logical_inspection"])
        if 0 <= total <= 1:
            confidence *= 100
            total *= 100
        elif all(
            0 <= component <= 1
            for component in (testing, code_review, logical_inspection)
        ):
            confidence *= 100
            total = testing * 40 + code_review * 30 + logical_inspection * 30

    if confidence < CONFIDENCE_GATE:
        fail(f"{ticket_id} confidence {confidence}% is below required gate {CONFIDENCE_GATE}%")

    if abs(total - confidence) > CONFIDENCE_BREAKDOWN_TOLERANCE:
        fail(f"{ticket_id} confidence {confidence}% does not match breakdown total {total}%")

    blockers = result.get("blockers")
    if blockers:
        fail(f"{ticket_id} reported blocker(s): {blockers}")

    tests = result.get("tests_proofs")
    if not isinstance(tests, list) or not tests:
        fail(f"{ticket_id} result must include at least one test/proof")

    def expected_nonzero_probe(test: object) -> bool:
        if not isinstance(test, dict):
            return False
        summary = str(test.get("summary") or "").lower()
        return summary.startswith("expected no-match")

    failed = failed_final_helper_proofs(tests, ticket, expected_nonzero_probe)
    if failed:
        fail(f"{ticket_id} reported failed test/proof command(s): {failed}")


def implementation_prompt(base_prompt: str, ticket: dict[str, str], packet_path: Path) -> str:
    return base_prompt + f"""

Use this compact ticket packet as the scoped execution packet for this ticket:

{packet_path}

The packet contains the exact ticket body, metadata, scope boundaries, and helper-owned proof commands. Use the full plan only to resolve contradictions or missing context; do not broaden the ticket beyond the packet.

Final response contract:

Return only JSON that satisfies the ticket result schema supplied by the Codex CLI `--output-schema` option.
The JSON must include `ticket_id` exactly `{ticket['id']}` and `target_repo` exactly `{ticket['target_repo']}`.
Set `status` to `blocked` if any blocker remains; otherwise set it to `complete`.
Do not include markdown outside the JSON object.
"""


def audit_prompt(ticket: dict[str, str], plan: Path, target_root: Path, packet_path: Path) -> str:
    return f"""Audit uncommitted changes for ticket {ticket['id']} - {ticket['title']}.

Plan: {plan}
Ticket packet: {packet_path}
Target repo: {ticket['target_repo']}
Target repo root: {target_root}

Do not edit files.
Audit exactly the target repo root above. Do not inspect or rely on a sibling checkout with the same repository name.
Audit for:

1. Completeness according to the ticket packet and {plan}.
2. Strict adherence to AGENTS.md and any other repository agent instructions.
3. Alignment with third-party documentation or SDK contracts for any external control flow.
4. Incorrectness, logical errors, regressions, security/trust-boundary risks, missing tests, repository-instruction violations, plan incompleteness, and incorrect target-repo scope.

Classify every finding with `scope`:

- `in_scope`: must be fixed before this ticket can pass.
- `adjacent_followup`: real issue, but outside this ticket's Primary invariant or Follow-up boundary; should become a follow-up ticket.
- `out_of_scope`: not relevant to this ticket and should not trigger repair.

Return only JSON that satisfies the audit result schema supplied by the Codex CLI `--output-schema` option.
The JSON must include `ticket_id` exactly `{ticket['id']}` and `target_repo` exactly `{ticket['target_repo']}`.
Set `status` to `fail` if there are `in_scope` findings. Set `status` to `pass` only when there are no `in_scope` findings. `adjacent_followup` findings may be returned with `status: pass` only when they do not block this ticket.
Do not include markdown outside the JSON object.
"""


def validate_audit_result(result: dict[str, object], ticket: dict[str, str]) -> None:
    ticket_id = ticket["id"]
    if result.get("ticket_id") != ticket_id:
        fail(f"{ticket_id} audit ticket_id mismatch: {result.get('ticket_id')!r}")
    if result.get("target_repo") != ticket["target_repo"]:
        fail(f"{ticket_id} audit target_repo mismatch: {result.get('target_repo')!r}")
    status = result.get("status")
    if status not in ("pass", "fail"):
        fail(f"{ticket_id} audit status must be pass or fail, got {status!r}")
    findings = result.get("findings")
    if not isinstance(findings, list):
        fail(f"{ticket_id} audit findings must be a list")
    for finding in findings:
        scope = finding.get("scope")
        if scope not in ("in_scope", "adjacent_followup", "out_of_scope"):
            fail(f"{ticket_id} audit finding has invalid scope: {scope!r}")
    in_scope = [finding for finding in findings if finding.get("scope") == "in_scope"]
    if status == "pass" and in_scope:
        fail(f"{ticket_id} audit status pass cannot include in_scope findings")
    if status == "fail" and not in_scope:
        fail(f"{ticket_id} audit status fail must include at least one in_scope finding")


def audit_repair_prompt(base_prompt: str, ticket: dict[str, str], plan: Path, target_root: Path, packet_path: Path, pass_number: int) -> str:
    return base_prompt + f"""

Audit/repair pass {pass_number} for ticket {ticket['id']} - {ticket['title']}.

Plan: {plan}
Ticket packet: {packet_path}
Target repo: {ticket['target_repo']}
Target repo root: {target_root}

Use this exact sequence:

1. Audit uncommitted ticket changes against the ticket packet, the plan, AGENTS.md, repository instructions, and relevant third-party SDK/documentation contracts.
2. Classify every finding with one of:
   - `in_scope`: must be fixed before this ticket can pass.
   - `adjacent_followup`: real issue outside this ticket's Primary invariant or Follow-up boundary.
   - `out_of_scope`: not relevant to this ticket.
3. If there are no `in_scope` findings, do not edit files. Return `status: "pass"` and `patched: false`.
4. If there are `in_scope` findings, repair only those findings and directly coupled code/tests needed to keep the ticket coherent. Do not repair `adjacent_followup` or `out_of_scope` findings. Return `status: "fail"` and `patched: true`.

This is one of at most three audit/repair passes. There will be no fourth audit. On pass 3, repair in-scope findings if you find them; the helper will run proof commands and final review will be the next independent review gate.

Return only JSON that satisfies the audit-repair result schema supplied by the Codex CLI `--output-schema` option.
The JSON must include `ticket_id` exactly `{ticket['id']}` and `target_repo` exactly `{ticket['target_repo']}`.
If `patched` is true, include changed files, tests/proofs, confidence, confidence_breakdown, blockers, and remaining gaps/risks using the same standards as ticket implementation.
Do not include markdown outside the JSON object.
"""


def require_patch_confidence(result: dict[str, object], ticket: dict[str, str], stage_label: str) -> None:
    ticket_id = ticket["id"]
    confidence = float(result.get("confidence", -1))
    breakdown = result.get("confidence_breakdown")
    if not isinstance(breakdown, dict):
        fail(f"{ticket_id} {stage_label} missing confidence_breakdown object")
    total = sum(float(breakdown[key]) for key in ("testing", "code_review", "logical_inspection"))
    if 0 <= confidence <= 1:
        testing = float(breakdown["testing"])
        code_review = float(breakdown["code_review"])
        logical_inspection = float(breakdown["logical_inspection"])
        if 0 <= total <= 1:
            confidence *= 100
            total *= 100
        elif all(0 <= component <= 1 for component in (testing, code_review, logical_inspection)):
            confidence *= 100
            total = testing * 40 + code_review * 30 + logical_inspection * 30
    if confidence < CONFIDENCE_GATE:
        fail(f"{ticket_id} {stage_label} confidence {confidence}% is below required gate {CONFIDENCE_GATE}%")
    if abs(total - confidence) > CONFIDENCE_BREAKDOWN_TOLERANCE:
        fail(f"{ticket_id} {stage_label} confidence {confidence}% does not match breakdown total {total}%")
    blockers = result.get("blockers")
    if blockers:
        fail(f"{ticket_id} {stage_label} reported blocker(s): {blockers}")
    tests = result.get("tests_proofs")
    if not isinstance(tests, list) or not tests:
        fail(f"{ticket_id} {stage_label} must include at least one test/proof when patched")
    failed = failed_final_helper_proofs(tests, ticket)
    if failed:
        fail(f"{ticket_id} {stage_label} reported failed helper-owned proof command(s): {failed}")


def validate_audit_repair_result(result: dict[str, object], ticket: dict[str, str]) -> None:
    validate_audit_result(result, ticket)
    ticket_id = ticket["id"]
    patched = result.get("patched")
    if not isinstance(patched, bool):
        fail(f"{ticket_id} audit/repair result patched must be boolean")
    in_scope = [finding for finding in result.get("findings", []) if finding.get("scope") == "in_scope"]
    if patched and not in_scope:
        fail(f"{ticket_id} audit/repair result patched true requires in_scope findings")
    if patched and result.get("status") != "fail":
        fail(f"{ticket_id} audit/repair result patched true must use status fail so another pass or final review records the repair")
    if not patched and result.get("status") == "fail":
        fail(f"{ticket_id} audit/repair result failed without a patch")
    if patched:
        files_changed = result.get("files_changed")
        if not isinstance(files_changed, list) or not files_changed:
            fail(f"{ticket_id} audit/repair result must include files_changed when patched")
        require_patch_confidence(result, ticket, "audit/repair")


def repair_prompt(base_prompt: str, ticket: dict[str, str], audit_result: dict[str, object], attempt: int, packet_path: Path) -> str:
    findings = [finding for finding in audit_result["findings"] if finding.get("scope") == "in_scope"]
    findings_json = json.dumps(findings, indent=2, sort_keys=True)
    return base_prompt + f"""

Repair attempt {attempt} for ticket {ticket['id']}.

Use this compact ticket packet as the scope authority for the repair:

{packet_path}

The read-only audit agent found these in-scope actionable findings:

```json
{findings_json}
```

Fix only these in-scope audit findings and any directly coupled issues required to keep the ticket coherent.
Do not fix `adjacent_followup` or `out_of_scope` audit findings in this repair attempt.
Do not commit changes.

Final response contract:

Return only JSON that satisfies the ticket result schema supplied by the Codex CLI `--output-schema` option.
The JSON must include `ticket_id` exactly `{ticket['id']}` and `target_repo` exactly `{ticket['target_repo']}`.
Set `status` to `blocked` if any blocker remains; otherwise set it to `complete`.
Do not include markdown outside the JSON object.
"""


def require_only_target_changed(target_name: str, target_roots: dict[str, Path], before: dict[str, str]) -> None:
    for name, repo in target_roots.items():
        if name == target_name:
            continue
        after = git_status(repo)
        if after != before[name]:
            fail(f"ticket for {target_name} changed non-target repo {name}")


def ticket_commit_exists(repo: Path, plan_abbrev: str, ticket_id: str) -> bool:
    return ticket_commit_sha(repo, plan_abbrev, ticket_id) is not None


def ticket_commit_sha(repo: Path, plan_abbrev: str, ticket_id: str) -> str | None:
    result = run(["git", "-C", str(repo), "log", "--format=%s"])
    require_success(result, f"git log in {repo}")
    prefix = f"{plan_abbrev} {ticket_id}: "
    subjects = result.stdout.splitlines()
    if not any(subject.startswith(prefix) for subject in subjects):
        return None
    sha_result = run(["git", "-C", str(repo), "log", "--format=%H%x00%s"])
    require_success(sha_result, f"git log with hashes in {repo}")
    for line in sha_result.stdout.splitlines():
        sha, _, subject = line.partition("\x00")
        if subject.startswith(prefix):
            return sha
    return None


def commit_ticket(repo: Path, message: str) -> str:
    add = run(["git", "-C", str(repo), "add", "-A"])
    require_success(add, f"git add in {repo}")
    diff_cached = run(["git", "-C", str(repo), "diff", "--cached", "--quiet"])
    if diff_cached.returncode == 0:
        fail(f"no staged changes to commit in {repo}")
    if diff_cached.returncode not in (0, 1):
        require_success(diff_cached, f"git diff --cached in {repo}")
    commit = run(["git", "-C", str(repo), "commit", "-m", message])
    if commit.returncode != 0:
        print(commit.stdout, end="")
        print(commit.stderr, end="", file=sys.stderr)
    require_success(commit, f"git commit in {repo}")
    return git_value(repo, ["rev-parse", "HEAD"], "git rev-parse HEAD")


def stop_active_child(state_file: Path, ticket_id: str) -> None:
    state = read_state(state_file)
    current = state.get("current") or {}
    if current.get("ticket_id") != ticket_id:
        fail(f"--stop ticket mismatch: state current={current.get('ticket_id')!r}, requested={ticket_id!r}")
    child = state.get("active_child")
    pid = child.get("pid") if isinstance(child, dict) else None
    if isinstance(pid, int) and process_alive(pid):
        terminate_process_group(pid)
    message = f"ticket {ticket_id} stopped by user request"
    update_ticket(state_file, ticket_id, status="blocked", stage=current.get("stage"), last_error=message)
    update_phase(
        state_file,
        "blocked",
        blocker={
            "ticket_id": ticket_id,
            "stage": current.get("stage"),
            "reason": "stopped_by_user",
            "pid": pid,
        },
        active_child=None,
    )
    append_event(
        state_file,
        {
            "phase": "ticket_loop",
            "ticket_id": ticket_id,
            "stage": current.get("stage"),
            "status": "stopped_by_user",
            "pid": pid,
            "message": message,
        },
    )


def write_split_recommendation(
    state_file: Path,
    ticket: dict[str, str],
    audit_result: dict[str, object] | None,
    plan: Path,
    target_root: Path,
) -> Path:
    ticket_log_root = logs_root(state_file) / ticket["id"]
    ticket_log_root.mkdir(parents=True, exist_ok=True)
    path = ticket_log_root / "split-recommendation.json"
    findings = []
    if audit_result:
        findings = [
            finding
            for finding in audit_result.get("findings", [])
            if isinstance(finding, dict) and finding.get("scope") == "in_scope"
        ]
    touched_files = sorted(
        {
            str(finding.get("file"))
            for finding in findings
            if finding.get("file")
        }
    )
    recommendation = {
        "ticket_id": ticket["id"],
        "target_repo": ticket["target_repo"],
        "title": ticket["title"],
        "plan": str(plan),
        "target_root": str(target_root),
        "reason": "audit_cap_reached",
        "audit_result": audit_result,
        "remaining_in_scope_findings": findings,
        "touched_files": touched_files,
        "suggested_boundaries": [
            {
                "source_finding": finding.get("description"),
                "required_fix": finding.get("required_fix"),
                "suggested_primary_invariant": finding.get("required_fix") or finding.get("description"),
                "suggested_touched_files": [finding.get("file")] if finding.get("file") else [],
            }
            for finding in findings
        ],
        "summary": audit_result.get("summary") if audit_result else None,
    }
    path.write_text(json.dumps(recommendation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    append_event(
        state_file,
        {
            "phase": "ticket_loop",
            "ticket_id": ticket["id"],
            "target_repo": ticket["target_repo"],
            "stage": "audit",
            "status": "split_recommendation",
            "path": str(path),
        },
    )
    return path


def daemon_command(argv: list[str], port: int) -> list[str]:
    command = [sys.executable, str(Path(__file__).resolve())]
    skip_next = False
    no_serve_status = any(arg == "--no-serve-status" for arg in argv[1:])
    for index, arg in enumerate(argv[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if arg == "--daemon":
            continue
        if arg == "--status-port":
            skip_next = True
            continue
        if arg.startswith("--status-port="):
            continue
        command.append(arg)
    if "--serve-status" not in command and not no_serve_status:
        command.append("--serve-status")
    if not no_serve_status:
        command.extend(["--status-port", str(port)])
    return command


def control_resume_command(argv: list[str], status_port: int | None = None) -> list[str]:
    command = [sys.executable, str(Path(__file__).resolve())]
    skip_next = False
    has_resume = False
    has_serve_status = False
    no_serve_status = False
    existing_status_port: int | None = None
    for index, arg in enumerate(argv[1:]):
        if skip_next:
            skip_next = False
            continue
        if arg in {"--daemon", "--reset-state", "--allow-dirty-resume"}:
            continue
        if arg == "--status-port":
            next_index = index + 2
            if next_index < len(argv):
                try:
                    existing_status_port = int(argv[next_index])
                except ValueError:
                    existing_status_port = None
            skip_next = True
            continue
        if arg.startswith("--status-port="):
            try:
                existing_status_port = int(arg.split("=", 1)[1])
            except ValueError:
                existing_status_port = None
            continue
        if arg == "--resume":
            has_resume = True
        if arg == "--serve-status":
            has_serve_status = True
        if arg == "--no-serve-status":
            no_serve_status = True
        command.append(arg)
    if not has_resume:
        command.append("--resume")
    if not has_serve_status and not no_serve_status:
        command.append("--serve-status")
    sticky_status_port = status_port if status_port is not None else existing_status_port
    if not no_serve_status and sticky_status_port and sticky_status_port > 0:
        command.extend(["--status-port", str(sticky_status_port)])
    return command


def persist_launch_metadata(
    state_file: Path,
    argv: list[str],
    cwd: Path,
    runtime_root: Path,
    status_port: int | None = None,
) -> None:
    state = read_state(state_file)
    launch = {
        "cwd": str(cwd),
        "resume_command": control_resume_command(argv, status_port=status_port),
        "runtime_root": str(runtime_root),
        "updated_at": now_iso(),
    }
    if status_port and status_port > 0:
        launch["status_port"] = status_port
        launch["status_url"] = f"http://127.0.0.1:{status_port}/"
    state["launch"] = launch
    write_state(state_file, state)


def command_has_option(command: list[str], option: str) -> bool:
    return any(arg == option or arg.startswith(option + "=") for arg in command)


def command_with_adopt_dirty_ticket(command: list[str], ticket_id: str | None) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for arg in command:
        if skip_next:
            skip_next = False
            continue
        if arg == "--adopt-dirty-ticket":
            skip_next = True
            continue
        if arg.startswith("--adopt-dirty-ticket="):
            continue
        cleaned.append(arg)
    if ticket_id is None:
        return cleaned
    return [*cleaned, "--adopt-dirty-ticket", ticket_id]


def request_pause_from_status(state_file: Path) -> dict[str, object]:
    state = read_state(state_file)
    phase = str(state.get("phase") or "")
    if phase == "complete":
        fail("cannot pause a completed ship-loop")
    set_requested_action(state_file, "pause")
    state = read_state(state_file)
    child = state.get("active_child")
    pid = child.get("pid") if isinstance(child, dict) else None
    log = child.get("log") if isinstance(child, dict) else None
    ticket_id = child.get("ticket_id") if isinstance(child, dict) else None
    stage = child.get("stage") if isinstance(child, dict) else None
    event: dict[str, object] = {
        "phase": "status_server",
        "status": "pause_requested",
        "message": "pause requested from localhost status page",
    }
    if isinstance(pid, int):
        event["pid"] = pid
        if process_alive(pid):
            terminate_process_group(pid)
            event["message"] = "pause requested; active child process group terminated"
    if isinstance(log, str):
        event["log"] = log
    if isinstance(ticket_id, str):
        event["ticket_id"] = ticket_id
    if isinstance(stage, str):
        event["stage"] = stage
    append_event(state_file, event)
    return {
        "requested_action": "pause",
        "pid": pid,
        "log": log,
        "message": event["message"],
    }


def request_close_status_page(state_file: Path) -> dict[str, object]:
    state = read_state(state_file)
    child = state.get("active_child")
    pid = child.get("pid") if isinstance(child, dict) else None
    if isinstance(pid, int) and process_alive(pid):
        fail("cannot close the status page while a child is active")

    phase = str(state.get("phase") or "")
    tickets = state.get("tickets") if isinstance(state.get("tickets"), list) else []
    has_held_ticket = any(
        isinstance(ticket, dict) and ticket.get("status") in {"blocked", "failed", "paused"}
        for ticket in tickets
    )
    if phase not in {"complete", "blocked", "failed", "paused"} and not state.get("blocker") and not has_held_ticket:
        fail(f"cannot close the status page while ship-loop is active: phase={phase!r}")

    append_event(
        state_file,
        {
            "phase": "status_server",
            "status": "close_requested",
            "message": "localhost status page close requested",
        },
    )
    return {"message": "status page closing"}


def launch_resume_from_status(state_file: Path, runtime_root: Path) -> dict[str, object]:
    state = read_state(state_file)
    active_child = state.get("active_child")
    if isinstance(active_child, dict):
        pid = active_child.get("pid")
        if isinstance(pid, int) and process_alive(pid):
            fail("cannot resume while a child is active")
        state["active_child"] = None
        write_state(state_file, state)
        append_event(
            state_file,
            {
                "phase": "status_server",
                "status": "cleared_stale_active_child",
                "pid": pid,
            },
        )
    elif active_child:
        fail("cannot resume while a child is active")
    phase = str(state.get("phase") or "")
    tickets = state.get("tickets") if isinstance(state.get("tickets"), list) else []
    has_resumable_ticket = any(
        isinstance(ticket, dict) and ticket.get("status") in RESUMABLE_TICKET_STATUSES
        for ticket in tickets
    )
    if phase not in RESUMABLE_PHASES and not has_resumable_ticket:
        fail(f"ship-loop is not in a resumable state: phase={phase!r}")
    launch = state.get("launch")
    if not isinstance(launch, dict) or not isinstance(launch.get("resume_command"), list):
        fail("ship-loop state does not contain launch.resume_command; resume from CLI once to refresh launch metadata")
    command = [str(part) for part in launch["resume_command"]]
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    command = command_with_adopt_dirty_ticket(command, str(current.get("ticket_id")) if current.get("ticket_id") else None)
    cwd = Path(str(launch.get("cwd") or Path.cwd()))
    log_path = runtime_root / "control-resume.log"
    set_requested_action(state_file, None)
    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
    append_event(
        state_file,
        {
            "phase": "status_server",
            "status": "resume_launched",
            "pid": process.pid,
            "log": str(log_path),
            "command": command,
        },
    )
    return {"pid": process.pid, "log": str(log_path), "command": command}


def launch_daemon(args: argparse.Namespace, state_file: Path, target_roots: dict[str, Path]) -> None:
    runtime_root = runtime_status_root(args, target_roots)
    runtime_root.mkdir(parents=True, exist_ok=True)
    if args.planning_repo_root is not None and args.planning_repo_root.is_dir():
        exclude_runtime_status_dir(args.planning_repo_root, runtime_root)
    port = args.status_port or free_port() if args.serve_status else 0
    command = daemon_command(sys.argv, port)
    log_path = runtime_root / "daemon.log"
    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
    url = f"http://127.0.0.1:{port}/" if args.serve_status else None
    (runtime_root / "daemon.json").write_text(
        json.dumps(
            {
                "pid": process.pid,
                "command": command,
                "state_file": str(state_file),
                "url": url,
                "log": str(log_path),
                "started_at": now_iso(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"SHIP_LOOP_STATE {state_file}", flush=True)
    if url:
        print(f"SHIP_LOOP_STATUS_URL {url}", flush=True)
    print(f"SHIP_LOOP_DAEMON_PID {process.pid}", flush=True)


def main() -> None:
    global ACTIVE_RUN_LOCK_FOR_EXIT
    global ACTIVE_STATE_FILE_FOR_EXIT
    global ACTIVE_STATUS_SERVER_FOR_EXIT

    parser = argparse.ArgumentParser(description="Run the deterministic ship-loop ticket implementation loop.")
    parser.add_argument("plan", nargs="?", type=Path)
    parser.add_argument("--ticket-template", type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--planning-repo-root", type=Path)
    parser.add_argument("--target-repo", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--plan-abbrev")
    parser.add_argument("--plan-slug")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--json", action="store_true", help="Accepted with --summary/--status; output is already JSON.")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--ticket")
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--daemon", action="store_true", help="Launch the ticket loop as a detached background helper and exit.")
    parser.add_argument("--serve-status", dest="serve_status", action="store_true", help="Serve a read-only localhost status page for this run.")
    parser.add_argument("--no-serve-status", dest="serve_status", action="store_false", help="Do not serve a localhost status page.")
    parser.add_argument("--status-port", type=int, default=0, help="Port for --serve-status; 0 chooses a free port.")
    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument("--open-browser", dest="open_browser", action="store_true", help="Open the status page in the default browser.")
    browser_group.add_argument("--no-open-browser", dest="open_browser", action="store_false", help="Do not open the status page in the default browser.")
    parser.add_argument("--codex-bin")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--sandbox", default="workspace-write")
    parser.add_argument("--approval-policy", default="never")
    parser.add_argument("--add-dir", action="append", default=[], type=Path)
    parser.add_argument("--max-repair-attempts", default=2, type=int)
    parser.add_argument("--allow-dirty-resume", action="store_true")
    parser.add_argument("--adopt-dirty-ticket")
    parser.add_argument("--heartbeat-seconds", default=180, type=int)
    parser.add_argument("--stale-warning-heartbeats", default=2, type=int)
    parser.add_argument("--stale-fail-heartbeats", default=8, type=int)
    parser.add_argument("--no-codex-json-events", dest="codex_json_events", action="store_false")
    parser.set_defaults(codex_json_events=True, serve_status=None, open_browser=None)
    args = parser.parse_args()

    if args.serve_status is None:
        args.serve_status = bool(args.resume or args.daemon)

    if args.status:
        if args.state_file is None:
            fail("--status requires --state-file")
        show_status(args.state_file.resolve())
        return
    if args.summary:
        if args.state_file is None:
            fail("--summary requires --state-file")
        show_summary(args.state_file.resolve())
        return
    if args.stop:
        if args.state_file is None:
            fail("--stop requires --state-file")
        if not args.ticket:
            fail("--stop requires --ticket")
        stop_active_child(args.state_file.resolve(), args.ticket)
        return

    missing = [
        name
        for name, value in (
            ("plan", args.plan),
            ("--ticket-template", args.ticket_template),
            ("--workspace-root", args.workspace_root),
            ("--planning-repo-root", args.planning_repo_root),
            ("--plan-abbrev", args.plan_abbrev),
        )
        if value is None
    ]
    if not args.target_repo:
        missing.append("--target-repo")
    if missing:
        fail("missing required argument(s): " + ", ".join(missing))
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
    args.plan = args.plan.resolve()
    args.ticket_template = args.ticket_template.resolve()
    args.workspace_root = args.workspace_root.resolve()
    args.planning_repo_root = args.planning_repo_root.resolve()
    args.add_dir = [path.resolve() for path in args.add_dir]
    apply_context_config(args)
    args.codex_bin = resolve_codex_bin(args.codex_bin)
    args.plan_slug = args.plan_slug or args.plan_abbrev
    state_file = args.state_file.resolve() if args.state_file else default_state_file(args.planning_repo_root, args.plan_slug)
    ACTIVE_STATE_FILE_FOR_EXIT = state_file
    emit_state_path(state_file)
    assert_no_quarantine(args.codex_bin, state_file)

    if not args.plan.is_file():
        fail(f"plan not found: {args.plan}")
    if not args.ticket_template.is_file():
        fail(f"ticket template not found: {args.ticket_template}")
    if args.max_repair_attempts < 0:
        fail("--max-repair-attempts must be non-negative")
    if args.max_repair_attempts > MAX_REPAIR_ATTEMPTS:
        fail(
            f"--max-repair-attempts cannot exceed {MAX_REPAIR_ATTEMPTS}; "
            f"each ticket is capped at {MAX_AUDIT_RUNS} total audit run(s)"
        )

    target_roots = dict(parse_target(value) for value in args.target_repo)
    if len(target_roots) != len(args.target_repo):
        fail("duplicate target repo names are not allowed")
    for name, root in target_roots.items():
        if not root.is_dir():
            fail(f"target repo root for {name} not found: {root}")
        require_linked_worktree(name, root)

    if args.daemon:
        launch_daemon(args, state_file, target_roots)
        return

    run_lock = RunLock(state_file.with_name("run.lock"))
    run_lock.acquire()
    ACTIVE_RUN_LOCK_FOR_EXIT = run_lock
    atexit.register(run_lock.release)

    tickets = extract_tickets(args.plan, sorted(target_roots))
    if args.resume:
        if not state_file.is_file():
            fail(f"--resume requested but state file does not exist: {state_file}")
        reconcile_resume(state_file, tickets, target_roots, args.plan_abbrev, args.adopt_dirty_ticket)
    elif args.allow_dirty_resume:
        dirty_repos = {
            name: status
            for name, repo in target_roots.items()
            if (status := git_status(repo)).strip()
        }
        if len(dirty_repos) > 1:
            fail(
                "--allow-dirty-resume permits at most one dirty target repo; found:\n"
                + "\n".join(f"{name}\n{status}" for name, status in dirty_repos.items())
            )
        if not state_file.exists() or args.reset_state:
            initialize_state(state_file, args, target_roots, tickets)
            append_event(state_file, {"phase": "ticket_loop", "status": "initialized", "plan_path": str(args.plan)})
    else:
        require_clean(target_roots)
        if state_file.exists() and not args.reset_state:
            fail(f"state file already exists; use --resume or --reset-state: {state_file}")
        initialize_state(state_file, args, target_roots, tickets)
        append_event(state_file, {"phase": "ticket_loop", "status": "initialized", "plan_path": str(args.plan)})

    runtime_root = runtime_status_root(args, target_roots)
    initial_status_port = args.status_port if args.serve_status and args.status_port > 0 else None
    persist_launch_metadata(state_file, sys.argv, Path.cwd(), runtime_root, initial_status_port)

    status_server: ThreadingHTTPServer | None = None
    if args.serve_status:
        exclude_runtime_status_dir(args.planning_repo_root, runtime_root)
        try:
            status_server, _ = start_status_server(
                state_file,
                runtime_root,
                args.status_port or 0,
                bool(args.open_browser),
            )
        except OSError as exc:
            append_event(state_file, {"phase": "status_server", "status": "failed", "message": str(exc)})
        else:
            ACTIVE_STATUS_SERVER_FOR_EXIT = status_server
            persist_launch_metadata(state_file, sys.argv, Path.cwd(), runtime_root, status_server.server_port)
            append_event(state_file, {"phase": "status_server", "status": "started", "url": f"http://127.0.0.1:{status_server.server_port}/"})

    update_repo_states(state_file, target_roots)
    first_target_root = target_roots[sorted(target_roots)[0]]
    check_pause_requested(state_file, stage="preflight")
    run_codex_preflight(args, state_file, first_target_root)
    check_pause_requested(state_file, stage="preflight")
    if not read_state(state_file).get("blocker"):
        update_phase(state_file, "ticket_loop")
        require_consistent_state(state_file, target_roots, args.plan_abbrev)
    ticket_schema = schema_path("ticket-result.schema.json")
    audit_repair_schema = schema_path("audit-repair-result.schema.json")
    log_root = logs_root(state_file)

    for ticket in tickets:
        ticket_id = ticket["id"]
        target_name = ticket["target_repo"]
        check_pause_requested(state_file, ticket_id=ticket_id, stage="ticket_loop")
        target_root = target_roots.get(target_name)
        if target_root is None:
            fail(f"{ticket_id} target repo is not configured: {target_name}")
        existing_commit = ticket_commit_sha(target_root, args.plan_abbrev, ticket_id)
        if existing_commit is not None:
            update_ticket(state_file, ticket_id, status="committed", stage="commit", commit=existing_commit, last_error=None)
            append_event(
                state_file,
                {
                    "phase": "ticket_loop",
                    "ticket_id": ticket_id,
                    "target_repo": target_name,
                    "stage": "commit",
                    "status": "skipped",
                    "commit": existing_commit,
                    "message": "ticket commit already exists",
                },
            )
            continue

        before = {name: git_status(root) for name, root in target_roots.items()}
        prompt = render_ticket_prompt(
            args.ticket_template,
            {
                "TICKET": ticket_id,
                "PATH_TO_PLAN_MARKDOWN": str(args.plan),
                "WORKSPACE_ROOT": str(args.workspace_root),
                "PLANNING_REPO_ROOT": str(args.planning_repo_root),
                "TARGET_REPO_ROOT": str(target_root),
                "TARGET_REPO_NAME": target_name,
            },
        )

        ticket_log_root = log_root / ticket_id
        packet_path = write_ticket_packet(state_file, args.plan, ticket, target_root)
        append_event(
            state_file,
            {
                "phase": "ticket_loop",
                "ticket_id": ticket_id,
                "target_repo": target_name,
                "stage": "packet",
                "status": "complete",
                "path": str(packet_path),
                "proof_commands": proof_commands(ticket),
            },
        )
        latest_audit_repair_attempt = latest_existing_audit_repair_attempt(state_file, ticket_id) if args.resume else None
        if latest_audit_repair_attempt is not None:
            update_ticket(
                state_file,
                ticket_id,
                status="audit_repairing",
                stage="audit_repair",
                attempt=latest_audit_repair_attempt,
                last_error=None,
            )
            append_event(
                state_file,
                {
                    "phase": "ticket_loop",
                    "ticket_id": ticket_id,
                    "target_repo": target_name,
                    "stage": "implementation",
                    "status": "skipped",
                    "attempt": 0,
                    "message": (
                        "resume found a later durable audit/repair result; "
                        "skipping implementation rerun and continuing from audit/repair"
                    ),
                },
            )
        else:
            check_pause_requested(state_file, ticket_id=ticket_id, stage="implementation", attempt=0)
            implementation_result = run_codex_agent(
                args,
                state_file,
                ticket,
                target_root,
                implementation_prompt(prompt, ticket, packet_path),
                ticket_schema,
                f"Codex implementation for {ticket_id}",
                args.sandbox,
                "implementation",
                "implementing",
                ticket_log_root / "implementation.log",
                0,
            )
            implementation_result_adopted = bool(implementation_result.pop("_ship_loop_adopted_result", False))
            try:
                require_ticket_complete(implementation_result, ticket)
            except SystemExit as exc:
                message = str(exc)
                if implementation_result_adopted:
                    archive_invalid_durable_result(
                        state_file,
                        durable_result_path(state_file, ticket_id, "implementation", 0),
                        message,
                        ticket=ticket,
                        stage="implementation",
                        attempt=0,
                    )
                    implementation_result = run_codex_agent(
                        args,
                        state_file,
                        ticket,
                        target_root,
                        implementation_prompt(prompt, ticket, packet_path),
                        ticket_schema,
                        f"Codex implementation for {ticket_id}",
                        args.sandbox,
                        "implementation",
                        "implementing",
                        ticket_log_root / "implementation.log",
                        0,
                    )
                    implementation_result.pop("_ship_loop_adopted_result", None)
                    try:
                        require_ticket_complete(implementation_result, ticket)
                    except SystemExit as rerun_exc:
                        record_validation_failure(
                            state_file,
                            ticket,
                            stage="implementation",
                            attempt=0,
                            log_path=ticket_log_root / "implementation.log",
                            message=str(rerun_exc),
                        )
                        raise
                else:
                    record_validation_failure(
                        state_file,
                        ticket,
                        stage="implementation",
                        attempt=0,
                        log_path=ticket_log_root / "implementation.log",
                        message=message,
                    )
                    raise
            require_only_target_changed(target_name, target_roots, before)
            run_proof_commands(state_file, ticket, target_root, "implementation", 0, block_on_failure=False)

        audit_passed = False
        final_pass_repaired = False
        last_audit_result: dict[str, object] | None = None
        for pass_number in range(1, MAX_AUDIT_RUNS + 1):
            check_pause_requested(state_file, ticket_id=ticket_id, stage="audit_repair", attempt=pass_number)
            pass_before = git_status(target_root)
            audit_result = run_codex_agent(
                args,
                state_file,
                ticket,
                target_root,
                audit_repair_prompt(prompt, ticket, args.plan, target_root, packet_path, pass_number),
                audit_repair_schema,
                f"Codex audit/repair for {ticket_id} pass {pass_number}",
                args.sandbox,
                "audit_repair",
                "audit_repairing",
                ticket_log_root / f"audit-repair-{pass_number}.log",
                pass_number,
            )
            audit_result_adopted = bool(audit_result.pop("_ship_loop_adopted_result", False))
            try:
                validate_audit_repair_result(audit_result, ticket)
            except SystemExit as exc:
                message = str(exc)
                if audit_result_adopted:
                    archive_invalid_durable_result(
                        state_file,
                        durable_result_path(state_file, ticket_id, "audit_repair", pass_number),
                        message,
                        ticket=ticket,
                        stage="audit_repair",
                        attempt=pass_number,
                    )
                    audit_result = run_codex_agent(
                        args,
                        state_file,
                        ticket,
                        target_root,
                        audit_repair_prompt(prompt, ticket, args.plan, target_root, packet_path, pass_number),
                        audit_repair_schema,
                        f"Codex audit/repair for {ticket_id} pass {pass_number}",
                        args.sandbox,
                        "audit_repair",
                        "audit_repairing",
                        ticket_log_root / f"audit-repair-{pass_number}.log",
                        pass_number,
                    )
                    audit_result.pop("_ship_loop_adopted_result", None)
                    try:
                        validate_audit_repair_result(audit_result, ticket)
                    except SystemExit as rerun_exc:
                        record_validation_failure(
                            state_file,
                            ticket,
                            stage="audit_repair",
                            attempt=pass_number,
                            log_path=ticket_log_root / f"audit-repair-{pass_number}.log",
                            message=str(rerun_exc),
                        )
                        raise
                    audit_result_adopted = False
                else:
                    record_validation_failure(
                        state_file,
                        ticket,
                        stage="audit_repair",
                        attempt=pass_number,
                        log_path=ticket_log_root / f"audit-repair-{pass_number}.log",
                        message=message,
                    )
                    raise
            patched = bool(audit_result.get("patched"))
            pass_after = git_status(target_root)
            if patched:
                if pass_after == pass_before:
                    append_event(
                        state_file,
                        {
                            "phase": "ticket_loop",
                            "ticket_id": ticket_id,
                            "target_repo": target_name,
                            "stage": "audit_repair",
                            "status": "patch_already_present" if audit_result_adopted else "patch_no_net_diff",
                            "attempt": pass_number,
                            "message": (
                                "audit/repair reported patched=true but the target worktree diff was unchanged; "
                                "continuing through helper-owned proofs and the next audit gate"
                            ),
                        },
                    )
                require_only_target_changed(target_name, target_roots, before)
                run_proof_commands(
                    state_file,
                    ticket,
                    target_root,
                    "audit_repair",
                    pass_number,
                    block_on_failure=pass_number == MAX_AUDIT_RUNS,
                )
            elif pass_after != pass_before:
                fail(f"{ticket_id} audit/repair pass {pass_number} changed files while reporting patched=false")
            require_only_target_changed(target_name, target_roots, before)
            record_followup_findings(state_file, ticket, audit_result)
            last_audit_result = audit_result
            if audit_result["status"] == "pass":
                audit_passed = True
                break
            append_event(
                state_file,
                {
                    "phase": "ticket_loop",
                    "ticket_id": ticket_id,
                    "target_repo": target_name,
                    "stage": "audit_repair",
                    "status": "repaired",
                    "attempt": pass_number,
                    "final_pass": pass_number == MAX_AUDIT_RUNS,
                },
            )
            if pass_number == MAX_AUDIT_RUNS:
                audit_passed = True
                final_pass_repaired = True
                state = read_state(state_file)
                for state_ticket in state.get("tickets", []):
                    if isinstance(state_ticket, dict) and state_ticket.get("id") == ticket_id:
                        state_ticket["final_pass_repaired"] = True
                        break
                write_state(state_file, state)
                break

        if not audit_passed:
            split_path = write_split_recommendation(state_file, ticket, last_audit_result, args.plan, target_root)
            message = (
                f"{ticket_id} audit/repair did not pass after {MAX_AUDIT_RUNS} pass(es). "
                "Do not run a fourth audit or expand the plan automatically. Remaining in-scope findings: "
                f"{last_audit_result}. Split recommendation: {split_path}"
            )
            update_ticket(state_file, ticket_id, status="blocked", stage="audit_repair", last_error=message)
            append_event(
                state_file,
                {
                    "phase": "ticket_loop",
                    "ticket_id": ticket_id,
                    "target_repo": target_name,
                    "stage": "audit_repair",
                    "status": "blocked",
                    "message": message,
                },
            )
            fail(message)
        if final_pass_repaired:
            append_event(
                state_file,
                {
                    "phase": "ticket_loop",
                    "ticket_id": ticket_id,
                    "target_repo": target_name,
                    "stage": "audit_repair",
                    "status": "final_pass_repaired",
                    "message": "third audit/repair pass patched in-scope findings; final review is the next independent review gate",
                },
            )

        check_pause_requested(state_file, ticket_id=ticket_id, stage="final", attempt=0)
        run_proof_commands(state_file, ticket, target_root, "final", 0, block_on_failure=True)
        check_pause_requested(state_file, ticket_id=ticket_id, stage="commit", attempt=0)

        diff_check = run(["git", "-C", str(target_root), "diff", "--check"])
        print(diff_check.stdout, end="")
        print(diff_check.stderr, end="", file=sys.stderr)
        require_success(diff_check, f"git diff --check for {ticket_id}")
        check_pause_requested(state_file, ticket_id=ticket_id, stage="commit", attempt=0)

        commit = commit_ticket(target_root, f"{args.plan_abbrev} {ticket_id}: {ticket['title']}")
        update_ticket(state_file, ticket_id, status="committed", stage="commit", commit=commit, last_error=None)
        update_repo_states(state_file, target_roots)
        append_event(
            state_file,
            {
                "phase": "ticket_loop",
                "ticket_id": ticket_id,
                "target_repo": target_name,
                "stage": "commit",
                "status": "complete",
                "commit": commit,
            },
        )

    update_phase(state_file, "complete", current=None)
    append_event(state_file, {"phase": "ticket_loop", "status": "complete"})


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if ACTIVE_STATE_FILE_FOR_EXIT is not None:
            hold_status_server_after_blocker(
                ACTIVE_STATE_FILE_FOR_EXIT,
                ACTIVE_STATUS_SERVER_FOR_EXIT,
                ACTIVE_RUN_LOCK_FOR_EXIT,
                str(exc),
            )
        raise

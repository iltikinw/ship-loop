#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def run_git_path(repo: Path, relative_path: str) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", relative_path],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        raise SystemExit(f"ERROR: git metadata path resolution failed in {repo}")
    return Path(result.stdout.strip()).resolve()


def default_state_file(planning_repo_root: Path, plan_slug: str) -> Path:
    return run_git_path(planning_repo_root, f"ship-loop/{plan_slug}/state.json")


def events_file(state_file: Path) -> Path:
    return state_file.with_name("events.jsonl")


def logs_root(state_file: Path) -> Path:
    return state_file.with_name("logs")


def read_state(state_file: Path) -> dict[str, Any]:
    if not state_file.is_file():
        raise FileNotFoundError(state_file)
    return json.loads(state_file.read_text(encoding="utf-8"))


def write_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    temp_file = state_file.with_name(f".{state_file.name}.tmp")
    temp_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_file, state_file)


def emit_state_path(state_file: Path) -> None:
    print(f"SHIP_LOOP_STATE {state_file}", flush=True)


def append_event(state_file: Path, event: dict[str, Any]) -> dict[str, Any]:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    normalized = {"timestamp": now_iso(), **event}
    line = compact_json(normalized)
    with events_file(state_file).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(f"SHIP_LOOP_EVENT {line}", flush=True)
    return normalized


def update_ticket(
    state_file: Path,
    ticket_id: str,
    *,
    status: str,
    stage: str | None = None,
    attempt: int | None = None,
    commit: str | None = None,
    log: str | None = None,
    last_error: str | None = None,
) -> None:
    state = read_state(state_file)
    for ticket in state["tickets"]:
        if ticket["id"] == ticket_id:
            ticket["status"] = status
            if stage is not None:
                ticket["stage"] = stage
            if attempt is not None:
                ticket["attempt"] = attempt
            if commit is not None:
                ticket["commit"] = commit
            if log is not None:
                ticket["log"] = log
            ticket["last_error"] = last_error
            ticket["updated_at"] = now_iso()
            state["current"] = {"ticket_id": ticket_id, "stage": stage or ticket.get("stage")}
            write_state(state_file, state)
            return
    raise SystemExit(f"ERROR: state does not contain ticket {ticket_id}")


def update_phase(state_file: Path, phase: str, **extra: Any) -> None:
    state = read_state(state_file)
    state["phase"] = phase
    state.update(extra)
    write_state(state_file, state)

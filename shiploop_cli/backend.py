from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
RUN_TICKET_LOOP = SCRIPTS_DIR / "run_ticket_loop.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_ticket_loop  # type: ignore[import-not-found]  # noqa: E402


class BackendError(RuntimeError):
    pass


def load_status_payload(state_file: Path) -> dict[str, Any]:
    payload = run_ticket_loop.status_payload(state_file.resolve())
    if not isinstance(payload, dict):
        raise BackendError(f"status_payload returned non-object for {state_file}")
    return payload


def pause_run(state_file: Path) -> dict[str, object]:
    return run_ticket_loop.request_pause_from_status(state_file.resolve())


def resume_run(state_file: Path, runtime_root: Path) -> dict[str, object]:
    runtime_root.mkdir(parents=True, exist_ok=True)
    return run_ticket_loop.launch_resume_from_status(state_file.resolve(), runtime_root.resolve())


def stop_run(state_file: Path, ticket_id: str) -> None:
    if not ticket_id:
        raise BackendError("stop requires a current ticket")
    proc = subprocess.run(
        [
            sys.executable,
            str(RUN_TICKET_LOOP),
            "--stop",
            "--state-file",
            str(state_file.resolve()),
            "--ticket",
            ticket_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise BackendError(detail or f"stop failed with exit code {proc.returncode}")

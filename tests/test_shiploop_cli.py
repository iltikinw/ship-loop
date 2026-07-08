from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path


def test_console_script_target() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["shiploop"] == "shiploop_cli.cli:main"


def test_help_command_smoke() -> None:
    proc = subprocess.run(
        ["python3", "-m", "shiploop_cli.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "usage:" in proc.stdout.lower()


def test_version_does_not_import_tui_app() -> None:
    proc = subprocess.run(
        [
            "python3",
            "-c",
            (
                "import sys; "
                "from shiploop_cli.cli import main; "
                "rc = main(['--version']); "
                "print('shiploop_cli.app' in sys.modules); "
                "raise SystemExit(rc)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.splitlines()[-1] == "False"


def test_roots_help_smoke() -> None:
    proc = subprocess.run(
        ["python3", "-m", "shiploop_cli.cli", "roots", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "usage:" in proc.stdout.lower()

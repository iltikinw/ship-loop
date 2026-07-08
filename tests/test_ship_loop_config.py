from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ship_loop_config import ShipLoopConfigError, load_ship_loop_config  # noqa: E402
from run_ticket_loop import apply_context_config  # noqa: E402


def write_config(root: Path, data: dict[str, object]) -> None:
    config = root / "agent-prompts" / "ship-loop.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps(data), encoding="utf-8")


def test_missing_context_config_uses_public_defaults(tmp_path: Path) -> None:
    config = load_ship_loop_config(tmp_path)

    assert config.codex_bin is None
    assert config.model == "gpt-5.5"
    assert config.reasoning_effort == "high"
    assert config.repo_display_suffixes == ()
    assert config.status_open_browser is True


def test_load_context_config_from_agent_prompts(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        {
            "codex_bin": "/opt/codex",
            "model": "custom-model",
            "reasoning_effort": "medium",
            "repo_display_suffixes": ["-internal"],
            "status_open_browser": False,
        },
    )

    config = load_ship_loop_config(tmp_path)

    assert config.codex_bin == "/opt/codex"
    assert config.model == "custom-model"
    assert config.reasoning_effort == "medium"
    assert config.repo_display_suffixes == ("-internal",)
    assert config.status_open_browser is False


def test_context_config_rejects_unknown_fields(tmp_path: Path) -> None:
    write_config(tmp_path, {"unknown": True})

    with pytest.raises(ShipLoopConfigError, match="unsupported ship-loop config field"):
        load_ship_loop_config(tmp_path)


def test_cli_args_override_context_config(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        {
            "codex_bin": "/opt/config-codex",
            "model": "config-model",
            "reasoning_effort": "medium",
            "status_open_browser": False,
        },
    )
    args = Namespace(
        workspace_root=tmp_path,
        codex_bin="/opt/cli-codex",
        model="cli-model",
        reasoning_effort="high",
        open_browser=True,
    )

    apply_context_config(args)

    assert args.codex_bin == "/opt/cli-codex"
    assert args.model == "cli-model"
    assert args.reasoning_effort == "high"
    assert args.open_browser is True


def test_context_config_fills_missing_cli_args(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        {
            "codex_bin": "/opt/config-codex",
            "model": "config-model",
            "reasoning_effort": "medium",
            "status_open_browser": False,
        },
    )
    args = Namespace(
        workspace_root=tmp_path,
        codex_bin=None,
        model=None,
        reasoning_effort=None,
        open_browser=None,
    )

    apply_context_config(args)

    assert args.codex_bin == "/opt/config-codex"
    assert args.model == "config-model"
    assert args.reasoning_effort == "medium"
    assert args.open_browser is False

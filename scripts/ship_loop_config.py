#!/usr/bin/env python3
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_RELATIVE_PATH = Path("agent-prompts") / "ship-loop.json"
DEFAULT_CODEX_BIN = "codex"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_REASONING_EFFORT = "high"
SUPPORTED_KEYS = {
    "codex_bin",
    "model",
    "reasoning_effort",
    "repo_display_suffixes",
    "status_open_browser",
}


class ShipLoopConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShipLoopConfig:
    codex_bin: str | None = None
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    repo_display_suffixes: tuple[str, ...] = ()
    status_open_browser: bool = True
    path: Path | None = None

    def state_payload(self) -> dict[str, object]:
        return {
            "repo_display_suffixes": list(self.repo_display_suffixes),
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "status_open_browser": self.status_open_browser,
        }


def load_ship_loop_config(root: Path | None) -> ShipLoopConfig:
    if root is None:
        return ShipLoopConfig()
    path = root.expanduser().resolve() / CONFIG_RELATIVE_PATH
    if not path.exists():
        return ShipLoopConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShipLoopConfigError(f"cannot read ship-loop config: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ShipLoopConfigError(f"ship-loop config must be a JSON object: {path}")
    unknown = sorted(set(raw) - SUPPORTED_KEYS)
    if unknown:
        raise ShipLoopConfigError(f"unsupported ship-loop config field(s) in {path}: {', '.join(unknown)}")
    return ShipLoopConfig(
        codex_bin=_optional_str(raw, "codex_bin", path),
        model=_str_or_default(raw, "model", DEFAULT_MODEL, path),
        reasoning_effort=_str_or_default(raw, "reasoning_effort", DEFAULT_REASONING_EFFORT, path),
        repo_display_suffixes=_string_tuple(raw, "repo_display_suffixes", path),
        status_open_browser=_bool_or_default(raw, "status_open_browser", True, path),
        path=path,
    )


def _optional_str(raw: dict[str, Any], key: str, path: Path) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ShipLoopConfigError(f"{key} must be a non-empty string or null in {path}")
    return value


def _str_or_default(raw: dict[str, Any], key: str, default: str, path: Path) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value:
        raise ShipLoopConfigError(f"{key} must be a non-empty string in {path}")
    return value


def _bool_or_default(raw: dict[str, Any], key: str, default: bool, path: Path) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ShipLoopConfigError(f"{key} must be a boolean in {path}")
    return value


def _string_tuple(raw: dict[str, Any], key: str, path: Path) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ShipLoopConfigError(f"{key} must be a list of non-empty strings in {path}")
    return tuple(value)

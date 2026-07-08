from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from shiploop_cli.discovery import (
    DiscoveryError,
    add_configured_root,
    deterministic_roots,
    load_configured_roots,
    remove_configured_root,
    resolve_slug,
)


def write_run(root: Path, slug: str, *, state_path: Path | None = None) -> Path:
    runtime = root / ".ship-loop" / slug
    runtime.mkdir(parents=True)
    state_file = state_path or (root / ".git" / "ship-loop" / slug / "state.json")
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_slug": slug,
                "plan_path": str(root / "plan.md"),
                "phase": "ticket_loop",
                "repos": {},
                "tickets": [],
                "current": None,
                "created_at": "2026-07-08T00:00:00Z",
                "updated_at": "2026-07-08T00:00:00Z",
                "planning_repo_root": str(root),
            }
        ),
        encoding="utf-8",
    )
    (runtime / "status-server.json").write_text(
        json.dumps({"pid": os.getpid(), "state_file": str(state_file), "url": "http://127.0.0.1:1234/"}),
        encoding="utf-8",
    )
    return state_file


def test_resolve_slug_from_explicit_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write_run(root, "demo")

    record = resolve_slug("demo", explicit_roots=[root], config_path=tmp_path / "missing.json")

    assert record.slug == "demo"
    assert record.status_url == "http://127.0.0.1:1234/"


def test_cwd_ancestor_discovery(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    child = root / "a" / "b"
    child.mkdir(parents=True)
    write_run(root, "demo")

    roots = deterministic_roots(cwd=child, config_path=tmp_path / "missing.json")

    assert root.resolve() in roots


def test_cwd_ancestor_discovery_precedes_explicit_roots(tmp_path: Path) -> None:
    cwd_root = tmp_path / "cwd-repo"
    explicit_root = tmp_path / "explicit-repo"
    child = cwd_root / "nested"
    (cwd_root / ".ship-loop").mkdir(parents=True)
    (explicit_root / ".ship-loop").mkdir(parents=True)
    child.mkdir()

    roots = deterministic_roots(
        cwd=child,
        explicit_roots=[explicit_root],
        config_path=tmp_path / "missing.json",
    )

    assert roots[:2] == [cwd_root.resolve(), explicit_root.resolve()]


def test_configured_roots_round_trip(tmp_path: Path) -> None:
    config = tmp_path / "config" / "roots.json"
    root = tmp_path / "workspace"
    (root / ".ship-loop").mkdir(parents=True)

    add_configured_root(root, config)
    assert load_configured_roots(config) == [root.resolve()]

    remove_configured_root(root, config)
    assert load_configured_roots(config) == []


def test_missing_slug_is_hard_error(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".ship-loop").mkdir(parents=True)

    with pytest.raises(DiscoveryError, match="ship-loop slug not found"):
        resolve_slug("missing", explicit_roots=[root], config_path=tmp_path / "missing.json")


def test_bad_registry_json_is_hard_error(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    runtime = root / ".ship-loop" / "demo"
    runtime.mkdir(parents=True)
    (runtime / "status-server.json").write_text("{bad", encoding="utf-8")

    with pytest.raises(DiscoveryError, match="cannot read status registry"):
        resolve_slug("demo", explicit_roots=[root], config_path=tmp_path / "missing.json")


def test_stale_registry_pid_is_hard_error(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write_run(root, "demo")
    registry = root / ".ship-loop" / "demo" / "status-server.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["pid"] = 99999999
    registry.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(DiscoveryError, match="stale status registry pid"):
        resolve_slug("demo", explicit_roots=[root], config_path=tmp_path / "missing.json")


def test_ambiguous_slug_is_hard_error(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    write_run(root_a, "demo")
    write_run(root_b, "demo")

    with pytest.raises(DiscoveryError, match="ambiguous ship-loop slug"):
        resolve_slug("demo", explicit_roots=[root_a, root_b], config_path=tmp_path / "missing.json")

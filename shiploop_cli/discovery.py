from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "shiploop" / "roots.json"


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunRecord:
    slug: str
    state_file: Path
    runtime_root: Path
    registry_file: Path | None = None
    status_url: str | None = None


def load_configured_roots(config_path: Path = CONFIG_PATH) -> list[Path]:
    if not config_path.exists():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryError(f"cannot read shiploop roots config: {config_path}: {exc}") from exc
    roots = data.get("roots") if isinstance(data, dict) else None
    if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
        raise DiscoveryError(f"invalid shiploop roots config: {config_path}")
    return [Path(root).expanduser() for root in roots]


def save_configured_roots(roots: list[Path], config_path: Path = CONFIG_PATH) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = sorted({str(root.expanduser().resolve()) for root in roots})
    tmp_path = config_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps({"roots": normalized}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(config_path)


def add_configured_root(root: Path, config_path: Path = CONFIG_PATH) -> list[Path]:
    resolved = _require_dir(root, "configured root").resolve()
    roots = load_configured_roots(config_path)
    if resolved not in {candidate.expanduser().resolve() for candidate in roots}:
        roots.append(resolved)
    save_configured_roots(roots, config_path)
    return load_configured_roots(config_path)


def remove_configured_root(root: Path, config_path: Path = CONFIG_PATH) -> list[Path]:
    resolved = root.expanduser().resolve()
    roots = [candidate.expanduser().resolve() for candidate in load_configured_roots(config_path)]
    remaining = [candidate for candidate in roots if candidate != resolved]
    if len(remaining) == len(roots):
        raise DiscoveryError(f"configured root not found: {resolved}")
    save_configured_roots(remaining, config_path)
    return load_configured_roots(config_path)


def deterministic_roots(
    *,
    explicit_roots: list[Path] | None = None,
    cwd: Path | None = None,
    config_path: Path = CONFIG_PATH,
) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()

    def add(root: Path) -> None:
        resolved = root.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)

    start = (cwd or Path.cwd()).expanduser().resolve()
    for ancestor in (start, *start.parents):
        if (ancestor / ".ship-loop").is_dir():
            add(ancestor)

    for root in explicit_roots or []:
        add(_require_shiploop_root(root, "explicit search root"))

    for root in load_configured_roots(config_path):
        add(_require_shiploop_root(root, "configured root"))

    return roots


def resolve_slug(
    slug: str,
    *,
    explicit_roots: list[Path] | None = None,
    cwd: Path | None = None,
    config_path: Path = CONFIG_PATH,
) -> RunRecord:
    if not slug or "/" in slug:
        raise DiscoveryError(f"invalid ship-loop slug: {slug!r}")
    records: list[RunRecord] = []
    roots = deterministic_roots(explicit_roots=explicit_roots, cwd=cwd, config_path=config_path)
    for root in roots:
        registry_file = root / ".ship-loop" / slug / "status-server.json"
        if registry_file.exists():
            records.append(_record_from_registry(slug, registry_file))
    unique: dict[Path, RunRecord] = {record.state_file.resolve(): record for record in records}
    if not unique:
        searched = "\n".join(str(root / ".ship-loop" / slug / "status-server.json") for root in roots)
        raise DiscoveryError(f"ship-loop slug not found: {slug}\nsearched:\n{searched}")
    if len(unique) > 1:
        choices = "\n".join(
            f"{record.state_file} via {record.registry_file}" for record in unique.values()
        )
        raise DiscoveryError(f"ambiguous ship-loop slug: {slug}\n{choices}")
    return next(iter(unique.values()))


def record_from_state_file(state_file: Path) -> RunRecord:
    resolved = _require_file(state_file, "state file").resolve()
    state = _read_json_object(resolved, "state file")
    slug = _require_str(state, "plan_slug", resolved)
    runtime_root = _runtime_root_from_state(state, resolved)
    return RunRecord(slug=slug, state_file=resolved, runtime_root=runtime_root)


def _record_from_registry(slug: str, registry_file: Path) -> RunRecord:
    data = _read_json_object(registry_file, "status registry")
    pid = data.get("pid")
    if not isinstance(pid, int) or not _process_alive(pid):
        raise DiscoveryError(f"stale status registry pid in {registry_file}: {pid!r}")
    state_file = Path(_require_str(data, "state_file", registry_file)).expanduser()
    state_file = _require_file(state_file, "state file").resolve()
    state = _read_json_object(state_file, "state file")
    state_slug = _require_str(state, "plan_slug", state_file)
    if state_slug != slug:
        raise DiscoveryError(
            f"status registry slug mismatch: requested {slug!r}, state has {state_slug!r}: {registry_file}"
        )
    status_url = data.get("url")
    if status_url is not None and not isinstance(status_url, str):
        raise DiscoveryError(f"invalid status URL in registry: {registry_file}")
    return RunRecord(
        slug=slug,
        state_file=state_file,
        runtime_root=_runtime_root_from_state(state, state_file),
        registry_file=registry_file.resolve(),
        status_url=status_url,
    )


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _runtime_root_from_state(state: dict[str, object], state_file: Path) -> Path:
    launch = state.get("launch")
    if isinstance(launch, dict) and isinstance(launch.get("runtime_root"), str):
        return Path(str(launch["runtime_root"])).expanduser().resolve()
    workspace_root = state.get("workspace_root")
    planning_repo_root = state.get("planning_repo_root")
    root = workspace_root if isinstance(workspace_root, str) else planning_repo_root
    if not isinstance(root, str):
        raise DiscoveryError(f"state lacks launch.runtime_root or root fields: {state_file}")
    return (Path(root).expanduser().resolve() / ".ship-loop" / _require_str(state, "plan_slug", state_file))


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DiscoveryError(f"invalid {label}: expected JSON object: {path}")
    return data


def _require_str(data: dict[str, object], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise DiscoveryError(f"missing or invalid {key!r} in {path}")
    return value


def _require_dir(path: Path, label: str) -> Path:
    resolved = path.expanduser()
    if not resolved.is_dir():
        raise DiscoveryError(f"{label} is not a directory: {resolved}")
    return resolved


def _require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser()
    if not resolved.is_file():
        raise DiscoveryError(f"{label} does not exist: {resolved}")
    return resolved


def _require_shiploop_root(path: Path, label: str) -> Path:
    root = _require_dir(path, label)
    if root.name == ".ship-loop":
        root = root.parent
    if not (root / ".ship-loop").is_dir():
        raise DiscoveryError(f"{label} does not contain .ship-loop: {root}")
    return root

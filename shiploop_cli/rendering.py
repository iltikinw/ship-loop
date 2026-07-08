from __future__ import annotations

from datetime import datetime
from typing import Any


class RenderError(RuntimeError):
    pass


def validate_payload(payload: dict[str, Any]) -> None:
    for key in ("state_file", "phase", "plan_slug", "tickets_total", "tickets_by_status", "tickets"):
        if key not in payload:
            raise RenderError(f"status payload missing {key!r}")
    if not isinstance(payload["tickets"], list):
        raise RenderError("status payload field 'tickets' must be a list")
    if not isinstance(payload["tickets_by_status"], dict):
        raise RenderError("status payload field 'tickets_by_status' must be an object")


def status_counts(payload: dict[str, Any]) -> str:
    counts = payload.get("tickets_by_status")
    if not isinstance(counts, dict):
        raise RenderError("tickets_by_status must be an object")
    if not counts:
        return "none"
    return "  ".join(f"{key} {counts[key]}" for key in sorted(counts))


def status_counts_table(payload: dict[str, Any]) -> str:
    counts = payload.get("tickets_by_status")
    if not isinstance(counts, dict):
        raise RenderError("tickets_by_status must be an object")
    rows = [("total", _value(payload.get("tickets_total")))]
    rows.extend((str(key), _value(counts[key])) for key in sorted(counts))
    return _kv("Tickets", rows)


def current_summary(payload: dict[str, Any]) -> str:
    current = payload.get("current")
    if not isinstance(current, dict):
        return "none"
    ticket = current.get("ticket_id") or "none"
    stage = current.get("stage") or "none"
    return f"{ticket} {stage}"


def compact_active_child(payload: dict[str, Any]) -> str:
    child = payload.get("active_child")
    if not isinstance(child, dict):
        return _kv("Active Child", [("status", "none")])
    elapsed = _seconds(child.get("elapsed_seconds"))
    inactive = _seconds(child.get("inactive_seconds"))
    log_age = _seconds(child.get("log_mtime_age_seconds"))
    rows = [
        ("ticket", _value(child.get("ticket_id"))),
        ("stage", _value(child.get("stage"))),
        ("pid", f"{_value(child.get('pid'))} {'alive' if child.get('pid_alive') else 'not alive'}"),
    ]
    if elapsed:
        rows.append(("elapsed", elapsed))
    if inactive:
        rows.append(("inactive", inactive))
    if log_age:
        rows.append(("log age", log_age))
    rows.append(("result", "yes" if child.get("result_exists") else "no"))
    if child.get("current_tool"):
        rows.append(("tool", str(child["current_tool"])))
    if child.get("last_structured_event_type"):
        rows.append(("event", str(child["last_structured_event_type"])))
    return _kv("Active Child", rows)


def compact_last_event(payload: dict[str, Any]) -> str:
    event = payload.get("last_event")
    if not isinstance(event, dict):
        return _kv("Last Event", [("status", "none")])
    rows = [
        ("status", _value(event.get("status"))),
        ("ticket", _value(event.get("ticket_id"))),
    ]
    if event.get("message"):
        rows.append(("message", _compact_event_message(str(event["message"]))))
    for key in ("created_at", "updated_at", "timestamp"):
        if event.get(key):
            rows.append(("time", compact_time(str(event[key]))))
            break
    return _kv("Last Event", rows)


def compact_blocker(payload: dict[str, Any]) -> str:
    blocker = payload.get("blocker")
    if not isinstance(blocker, dict):
        return _kv("Blocker", [("status", "none")])
    rows = [
        ("ticket", _value(blocker.get("ticket_id"))),
        ("stage", _value(blocker.get("stage"))),
    ]
    for key in ("reason", "message", "pid"):
        value = blocker.get(key)
        if value is not None:
            rows.append((key, str(value)))
    return _kv("Blocker", rows)


def ticket_table(payload: dict[str, Any]) -> str:
    tickets = payload.get("tickets")
    if not isinstance(tickets, list):
        raise RenderError("tickets must be a list")
    suffixes = _repo_display_suffixes(payload)
    rows = [("Ticket", "Status", "Stage", "Repo", "Title", "Commit")]
    for raw in tickets:
        if not isinstance(raw, dict):
            raise RenderError("ticket row must be an object")
        rows.append(
            (
                _value(raw.get("id")),
                _value(raw.get("status")),
                _value(raw.get("stage")),
                _compact_repo(_value(raw.get("target_repo")), suffixes),
                _value(raw.get("title")),
                short_sha(raw.get("commit")),
            )
        )
    caps = [8, 14, 14, 18, 34, 7]
    widths = [min(caps[index], max(len(row[index]) for row in rows)) for index in range(len(rows[0]))]
    rendered = []
    for row_index, row in enumerate(rows):
        cells = [
            _clip(value, widths[index]).ljust(widths[index])
            for index, value in enumerate(row[:-1])
        ]
        cells.append(_clip(row[-1], widths[-1]))
        rendered.append("  ".join(cells).rstrip())
        if row_index == 0:
            rendered.append("  ".join("-" * width for width in widths))
    return "\n".join(rendered)


def short_sha(value: object) -> str:
    text = _value(value)
    return text[:7] if text not in {"", "None", "none"} else ""


def _value(value: object) -> str:
    return "none" if value is None else str(value)


def _compact_event_message(message: str) -> str:
    return message.partition("; log: ")[0]


def _clip(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 3:
        return "..."[:width]
    return value[: width - 3] + "..."


def kv_table(title: str, rows: list[tuple[str, object]]) -> str:
    return _kv(title, [(label, _value(value)) for label, value in rows])


def _kv(title: str, rows: list[tuple[str, str]]) -> str:
    if not rows:
        return title
    width = max(len(label) for label, _value_ in rows)
    return "\n".join([title, "-" * len(title), *(f"{label:<{width}}  {value}" for label, value in rows)])


def _seconds(value: object) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    seconds = int(value)
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def compact_path(path: str) -> str:
    parts = path.split("/")
    if len(parts) <= 4:
        return path
    return ".../" + "/".join(parts[-3:])


def compact_time(value: object) -> str:
    text = _value(value)
    parsed = _parse_timestamp(text)
    if parsed is None:
        return text
    return parsed.astimezone().strftime("%H:%M:%S %Z")


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _repo_display_suffixes(payload: dict[str, Any]) -> tuple[str, ...]:
    config = payload.get("config")
    if not isinstance(config, dict):
        return ()
    suffixes = config.get("repo_display_suffixes")
    if not isinstance(suffixes, list) or not all(isinstance(item, str) for item in suffixes):
        return ()
    return tuple(suffix for suffix in suffixes if suffix)


def _compact_repo(repo: str, suffixes: tuple[str, ...]) -> str:
    for suffix in suffixes:
        if repo.endswith(suffix):
            return repo[: -len(suffix)]
    return repo

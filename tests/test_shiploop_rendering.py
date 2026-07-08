from __future__ import annotations

from shiploop_cli.rendering import (
    compact_active_child,
    compact_last_event,
    compact_time,
    current_summary,
    status_counts,
    status_counts_table,
    ticket_table,
    validate_payload,
)


def payload() -> dict[str, object]:
    return {
        "state_file": "/tmp/state.json",
        "phase": "ticket_loop",
        "plan_slug": "demo",
        "tickets_total": 1,
        "tickets_by_status": {"pending": 1},
        "current": {"ticket_id": "D-01", "stage": "implementation"},
        "blocker": None,
        "active_child": {
            "ticket_id": "D-01",
            "stage": "implementation",
            "pid": 123,
            "pid_alive": True,
            "log_mtime_age_seconds": 2,
            "result_exists": False,
            "log": "/tmp/log",
        },
        "tickets": [
            {
                "id": "D-01",
                "target_repo": "repo",
                "title": "Do work",
                "status": "pending",
                "stage": None,
                "commit": None,
            }
        ],
        "last_event": {
            "phase": "ticket_loop",
            "stage": "implementation",
            "status": "started",
            "ticket_id": "D-01",
            "message": "started work",
            "log": "/tmp/log",
        },
        "updated_at": "2026-07-08T00:00:00Z",
    }


def test_validate_payload_accepts_status_payload_shape() -> None:
    validate_payload(payload())


def test_concise_summaries() -> None:
    data = payload()

    assert status_counts(data) == "pending 1"
    assert "Tickets\n-------" in status_counts_table(data)
    assert "total    1" in status_counts_table(data)
    assert current_summary(data) == "D-01 implementation"
    assert "pid      123 alive" in compact_active_child(data)
    assert ".../tmp/log" not in compact_active_child(data)
    assert "status   started" in compact_last_event(data)
    assert "where" not in compact_last_event(data)
    assert "message  started work" in compact_last_event(data)


def test_ticket_table_contains_expected_columns() -> None:
    table = ticket_table(payload())

    assert "Ticket" in table
    assert "Repo" in table
    assert "D-01" in table
    assert all(not line.endswith(" ") for line in table.splitlines())


def test_last_event_message_hides_log_suffix() -> None:
    data = payload()
    data["last_event"] = {
        "phase": "ticket_loop",
        "status": "error",
        "ticket_id": "D-01",
        "message": "failed audit; log: /tmp/ship-loop/log.txt",
    }

    rendered = compact_last_event(data)

    assert "message  failed audit" in rendered
    assert "; log:" not in rendered
    assert "/tmp/ship-loop/log.txt" not in rendered


def test_ticket_table_uses_configured_repo_display_suffixes() -> None:
    data = payload()
    data["config"] = {"repo_display_suffixes": ["-internal"]}
    data["tickets"] = [
        {
            "id": "D-01",
            "target_repo": "api-service-internal",
            "title": "Do work",
            "status": "pending",
            "stage": None,
            "commit": None,
        }
    ]

    table = ticket_table(data)

    assert "api-service" in table
    assert "api-service-internal" not in table


def test_compact_time_uses_local_timezone() -> None:
    rendered = compact_time("2026-07-08T03:05:39Z")

    assert rendered.endswith(("EDT", "EST"))
    assert rendered != "03:05:39Z"

from __future__ import annotations

from pathlib import Path

import pytest
from rich.text import Text

from shiploop_cli import app as app_module
from shiploop_cli.app import ShiploopApp
from shiploop_cli.discovery import RunRecord


def payload(status: str = "first") -> dict[str, object]:
    return {
        "state_file": "/tmp/state.json",
        "phase": "ticket_loop",
        "plan_slug": "demo",
        "tickets_total": 1,
        "tickets_by_status": {"pending": 1},
        "current": {"ticket_id": "D-01", "stage": status},
        "blocker": None,
        "active_child": None,
        "tickets": [
            {
                "id": "D-01",
                "target_repo": "repo",
                "title": "Do work",
                "status": "pending",
                "stage": status,
                "commit": None,
            }
        ],
        "last_event": {"phase": "ticket_loop", "status": status, "ticket_id": "D-01"},
        "updated_at": "2026-07-08T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_refresh_hotkey_loads_latest_payload(tmp_path: Path) -> None:
    calls = 0

    def load(_state_file: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return payload("first" if calls == 1 else "second")

    record = RunRecord(
        slug="demo",
        state_file=tmp_path / "state.json",
        runtime_root=tmp_path / ".ship-loop" / "demo",
        status_url="http://127.0.0.1:1234/",
    )
    app = ShiploopApp(record, payload_loader=load)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.query_one("#content-scroll") is not None
        assert app.query_one("#title").region.x == app.query_one("#tickets").region.x
        assert app.query_one("#title").region.width == app.query_one("#tickets").region.width
        title = str(app.query_one("#title").render())
        assert " ship loop " in title
        assert title.startswith("─")
        assert title.endswith("─")
        assert len(list(app.query("#refresh-button"))) == 0
        assert "first" in str(app.query_one("#header").renderable)
        assert "port" not in str(app.query_one("#header").renderable)
        assert "web      http://127.0.0.1:1234/" in str(app.query_one("#header").renderable)
        tickets = app.query_one("#tickets").renderable
        assert isinstance(tickets, Text)
        assert tickets.no_wrap is True
        assert str(app.query_one("#message").renderable) == ""

        await pilot.press("r")
        await pilot.pause()

        assert calls == 2
        assert "second" in str(app.query_one("#header").renderable)


@pytest.mark.asyncio
async def test_control_buttons_call_helper_contracts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        app_module.backend,
        "pause_run",
        lambda state_file: calls.append(("pause", state_file)) or {"message": "paused"},
    )
    monkeypatch.setattr(
        app_module.backend,
        "resume_run",
        lambda state_file, runtime_root: calls.append(("resume", (state_file, runtime_root)))
        or {"message": "resumed"},
    )
    monkeypatch.setattr(
        app_module.webbrowser,
        "open",
        lambda url: calls.append(("open", url)) or True,
    )

    record = RunRecord(
        slug="demo",
        state_file=tmp_path / "state.json",
        runtime_root=tmp_path / ".ship-loop" / "demo",
        status_url="http://127.0.0.1:1234/",
    )
    app = ShiploopApp(record, payload_loader=lambda _state_file: payload("implementation"))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        await pilot.click("#pause-button")
        await pilot.click("#resume-button")
        assert len(list(app.query("#stop-button"))) == 0
        await pilot.click("#open-web-button")
        await pilot.pause()

    assert ("pause", record.state_file) in calls
    assert ("resume", (record.state_file, record.runtime_root)) in calls
    assert ("open", "http://127.0.0.1:1234/") in calls


@pytest.mark.asyncio
async def test_refresh_preserves_bottom_scroll(tmp_path: Path) -> None:
    many_tickets = payload("implementation")
    many_tickets["tickets"] = [
        {
            "id": f"D-{index:02d}",
            "target_repo": "repo",
            "title": f"Do work {index}",
            "status": "pending",
            "stage": "implementation",
            "commit": None,
        }
        for index in range(40)
    ]
    many_tickets["tickets_total"] = 40
    record = RunRecord(
        slug="demo",
        state_file=tmp_path / "state.json",
        runtime_root=tmp_path / ".ship-loop" / "demo",
        status_url="http://127.0.0.1:1234/",
    )
    app = ShiploopApp(record, payload_loader=lambda _state_file: many_tickets)

    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        scroll = app.query_one("#content-scroll")
        scroll.scroll_end(animate=False)
        await pilot.pause()
        before = float(scroll.scroll_y)

        app.action_refresh()
        await pilot.pause()

        assert float(scroll.scroll_y) == before

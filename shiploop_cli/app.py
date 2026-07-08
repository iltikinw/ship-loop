from __future__ import annotations

import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from shiploop_cli import backend
from shiploop_cli.discovery import RunRecord
from shiploop_cli.rendering import (
    compact_active_child,
    compact_blocker,
    compact_last_event,
    compact_path,
    compact_time,
    current_summary,
    kv_table,
    status_counts_table,
    ticket_table,
    validate_payload,
)


PayloadLoader = Callable[[Path], dict[str, Any]]


@dataclass(frozen=True)
class ScrollSnapshot:
    y: float
    at_bottom: bool


class ControlButton(Static):
    def __init__(self, label: str, *, id: str) -> None:
        super().__init__(
            Text(label, justify="center", no_wrap=True),
            id=id,
            classes="control-button",
            expand=False,
        )

    def on_click(self, _event: events.Click) -> None:
        handler = getattr(self.app, "handle_control")
        handler(str(self.id))


class TitleLine(Static):
    def render(self) -> str:
        title = " ship loop "
        width = max(0, int(self.size.width))
        if width <= len(title):
            return title.strip()[:width]
        left = (width - len(title)) // 2
        right = width - len(title) - left
        return "─" * left + title + "─" * right


class ShiploopApp(App[None]):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True, priority=True),
        Binding("p", "pause", "Pause", show=True, priority=True),
        Binding("s", "resume", "Resume", show=True, priority=True),
        Binding("o", "open_web", "Open Web", show=True, priority=True),
        Binding("q", "quit", "Quit", show=True, priority=True),
    ]

    def __init__(
        self,
        record: RunRecord,
        *,
        payload_loader: PayloadLoader = backend.load_status_payload,
    ) -> None:
        super().__init__()
        self.record = record
        self.payload_loader = payload_loader
        self.payload: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="shiploop-root"):
            with VerticalScroll(id="content-scroll"):
                yield TitleLine(id="title")
                with Horizontal(id="top-grid"):
                    yield Static("", id="header")
                    yield Static("", id="summary")
                with Horizontal(id="details"):
                    yield Static("", id="active-child")
                    yield Static("", id="last-event")
                    yield Static("", id="blocker")
                yield Static("", id="tickets")
                with Horizontal(id="controls"):
                    yield ControlButton("Pause", id="pause-button")
                    yield ControlButton("Resume", id="resume-button")
                    yield Static("", id="controls-spacer")
                    yield ControlButton("Open", id="open-web-button")
                    yield ControlButton("Quit", id="quit-button")
            yield Static("", id="message")

    def on_mount(self) -> None:
        self.action_refresh()
        self.set_interval(1.0, self.action_refresh)

    def handle_control(self, button_id: str) -> None:
        if button_id == "pause-button":
            self.action_pause()
        elif button_id == "resume-button":
            self.action_resume()
        elif button_id == "open-web-button":
            self.action_open_web()
        elif button_id == "quit-button":
            self.exit()

    def action_refresh(self) -> None:
        try:
            self.payload = self.payload_loader(self.record.state_file)
            validate_payload(self.payload)
            self._render_payload()
        except Exception as exc:
            self._message(f"refresh failed: {exc}")

    def action_pause(self) -> None:
        try:
            backend.pause_run(self.record.state_file)
            self.action_refresh()
            self._message("pause requested")
        except Exception as exc:
            self._message(f"pause failed: {exc}")

    def action_resume(self) -> None:
        try:
            backend.resume_run(self.record.state_file, self.record.runtime_root)
            self.action_refresh()
            self._message("resume launched")
        except Exception as exc:
            self._message(f"resume failed: {exc}")

    def action_stop(self) -> None:
        try:
            ticket_id = self._current_ticket_id()
            backend.stop_run(self.record.state_file, ticket_id)
            self.action_refresh()
            self._message(f"stop requested for {ticket_id}")
        except Exception as exc:
            self._message(f"stop failed: {exc}")

    def action_open_web(self) -> None:
        if not self.record.status_url:
            self._message("open web failed: no status URL for this run")
            return
        try:
            webbrowser.open(self.record.status_url)
            self._message(f"opened {self.record.status_url}")
        except Exception as exc:
            self._message(f"open web failed: {exc}")

    def _render_payload(self) -> None:
        if self.payload is None:
            return
        snapshot = self._capture_scroll_snapshot()
        slug = self.payload.get("plan_slug")
        phase = self.payload.get("phase")
        updated = compact_time(self.payload.get("updated_at"))
        current = current_summary(self.payload)
        self.query_one("#header", Static).update(
            kv_table(
                "Run",
                [
                    ("slug", slug),
                    ("phase", phase),
                    ("current", current),
                    ("updated", updated),
                    ("state", compact_path(str(self.record.state_file))),
                    ("web", self.record.status_url or "none"),
                ],
            )
        )
        self.query_one("#summary", Static).update(status_counts_table(self.payload))
        self.query_one("#tickets", Static).update(
            Text(ticket_table(self.payload), no_wrap=True, overflow="ellipsis")
        )
        self.query_one("#active-child", Static).update(compact_active_child(self.payload))
        self.query_one("#last-event", Static).update(compact_last_event(self.payload))
        self.query_one("#blocker", Static).update(compact_blocker(self.payload))
        self.call_after_refresh(self._restore_scroll_snapshot, snapshot)

    def _current_ticket_id(self) -> str:
        if self.payload is None:
            self.payload = self.payload_loader(self.record.state_file)
        current = self.payload.get("current")
        if not isinstance(current, dict) or not isinstance(current.get("ticket_id"), str):
            raise RuntimeError("stop requires a current ticket")
        return str(current["ticket_id"])

    def _message(self, message: str) -> None:
        self.query_one("#message", Static).update(message)

    def _capture_scroll_snapshot(self) -> ScrollSnapshot:
        container = self.query_one("#content-scroll", VerticalScroll)
        max_y = float(container.max_scroll_y)
        y = float(container.scroll_y)
        return ScrollSnapshot(y=y, at_bottom=max_y <= 0 or y >= max_y - 0.5)

    def _restore_scroll_snapshot(self, snapshot: ScrollSnapshot) -> None:
        container = self.query_one("#content-scroll", VerticalScroll)
        target = float(container.max_scroll_y) if snapshot.at_bottom else min(snapshot.y, float(container.max_scroll_y))
        container._scroll_to(y=max(0.0, round(target)), animate=False, force=True)

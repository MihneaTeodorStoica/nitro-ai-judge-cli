"""Keyboard-first Textual interface for Nitro AI Judge."""

from __future__ import annotations

import asyncio
from collections import deque
import re
import time
from dataclasses import dataclass, replace
import os
import threading
from typing import Any, Callable
import webbrowser

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button,
    Footer,
    ContentSwitcher,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    OptionList,
    SelectionList,
    Static,
    Tab,
    Tabs,
)

from .api import (
    AuthenticationRedirect,
    AuthenticationRequired,
    do_login,
    ensure_fresh_state,
    get_auth,
    refresh_saved_tokens,
    save_token_state,
)
from .config import DEFAULT_SUBMISSION_PAGE_SIZE, runtime
from .contests import (
    download_task_data,
    load_competitions,
    load_task_file_categories,
    load_task_view,
    load_tasks,
)
from .play_manager_client import ManagerClient
from .play import _migrate_manager_if_needed
from .state import (
    CredentialsError,
    cached_items,
    load_context,
    load_state,
    selected_contest,
    selected_submission,
    selected_task,
    set_contest,
    set_submission,
    set_task,
    update_cache,
)
from .submissions import (
    create_submission,
    load_submission,
    load_submissions,
    set_submission_final,
    submission_score,
)
from .ui import format_datetime_ms
from .tui_paths import PathInput, expand_path


PENDING_STATES = {"created", "in queue", "pending", "processing", "queued", "running"}
SUBMISSION_POLL_INTERVAL = 3.0
TUI_CONTEST_PAGE_SIZE = 100
ACCENT = "#E57C5D"

NAIJ_THEME = Theme(
    name="naij",
    primary=ACCENT,
    secondary="#AAB3CC",
    accent=ACCENT,
    foreground="#F5F2EB",
    background="#222A45",
    surface="#2A3454",
    panel="#333D60",
    boost="#333D60",
    success="#7FBF8E",
    warning="#E1B866",
    error="#E06C75",
    variables={"muted": "#AAB3CC", "border": "#46516E"},
)

MONO_THEME = Theme(
    name="naij-mono",
    primary="#FFFFFF",
    secondary="#BFBFBF",
    accent="#FFFFFF",
    foreground="#FFFFFF",
    background="#000000",
    surface="#000000",
    panel="#000000",
    boost="#000000",
    success="#FFFFFF",
    warning="#FFFFFF",
    error="#FFFFFF",
    variables={"muted": "#BFBFBF", "border": "#BFBFBF"},
)


def contest_ref(contest: dict[str, Any]) -> tuple[str, str]:
    return (
        str(contest.get("organizationSlug") or contest.get("organization") or ""),
        str(contest.get("competitionSlug") or contest.get("slug") or ""),
    )


def contest_label(contest: dict[str, Any]) -> Text:
    org, comp = contest_ref(contest)
    label = Text(str(contest.get("title") or comp or "?"), style="bold")
    label.append(f"\n{org}/{comp}", style="dim")
    return label


def task_label(task: dict[str, Any], number: int) -> Text:
    synopsis = str(task.get("synopsis") or "").strip()
    label = Text(f"{number}. {task.get('title') or '?'}", style="bold")
    if synopsis:
        label.append(f"\n{synopsis}", style="dim")
    return label


def submission_label(submission: dict[str, Any]) -> str:
    mode = str(submission.get("_mode") or (
        "complete" if "completeTaskScore" in submission and "partialTaskScore" not in submission else "partial"
    ))
    short_id = str(submission.get("id") or "?").split("-")[-1]
    state = str(submission.get("state") or "unknown")
    final = " · final" if submission.get("isFinal") else ""
    return (
        f"{short_id} · {submission_score(submission, mode)}\n"
        f"{format_datetime_ms(submission.get('timestamp'))} · {state}{final}"
    )


def submission_details(submission: dict[str, Any]) -> str:
    lines = [
        f"Submission: {submission.get('id') or '?'}",
        f"User: {submission.get('username') or '?'}",
        f"Time: {format_datetime_ms(submission.get('timestamp'))}",
        f"State: {submission.get('state') or '?'}",
        f"Final: {'yes' if submission.get('isFinal') else 'no'}",
        f"Verdict: {submission.get('verdictMessage') or '—'}",
        f"Partial: {submission_score(submission, 'partial')}",
    ]
    if "completeTaskScore" in submission:
        lines.append(f"Complete: {submission_score(submission, 'complete')}")
    if submission.get("note"):
        lines.append(f"Note: {submission['note']}")
    subtasks = submission.get("subtasks") or []
    if subtasks:
        lines.extend(("", "Subtasks"))
        partial = submission.get("partialSubtaskScores") or []
        complete = submission.get("completeSubtaskScores") or []
        for index, subtask in enumerate(subtasks):
            if not isinstance(subtask, dict):
                continue
            maximum = subtask.get("maximumScore") or subtask.get("maxScore") or "?"
            pscore = partial[index] if index < len(partial) else "?"
            line = f"{index + 1}. partial {pscore}/{maximum}"
            if index < len(complete):
                line += f" · complete {complete[index]}/{maximum}"
            if subtask.get("metricName"):
                line += f" · {subtask['metricName']}"
            lines.append(line)
    lines.extend(
        (
            "",
            "Tab/Shift-Tab: toggle the list and detail scroller",
            "j/k or Up/Down: scroll the detail",
        )
    )
    return "\n".join(str(line) for line in lines)


def play_details(status: dict[str, Any]) -> Text:
    heading_style = (
        "bold" if os.environ.get("NO_COLOR") is not None else f"bold {ACCENT}"
    )
    text = Text()
    text.append("LOCAL PLAY\n", style=heading_style)
    text.append(f"[{str(status.get('state') or 'unknown').upper()}]\n", style="bold")
    for heading, rows in (
        (
            "Connections",
            (
                ("Manager", status.get("manager_url") or "unavailable"),
                ("Jupyter", status.get("jupyter_url") or "—"),
                ("Proxy", status.get("proxy_url") or "—"),
            ),
        ),
        (
            "Runtime",
            (
                ("GPU", status.get("gpu") or "—"),
                ("Images", status.get("images") or "—"),
            ),
        ),
        (
            "Paths",
            (
                ("Workspace", status.get("workspace") or "—"),
                ("Health", status.get("service_health") or "unknown"),
            ),
        ),
    ):
        text.append(f"\n{heading}\n", style=heading_style)
        for label, value in rows:
            text.append(f"{label:<11}", style="dim")
            text.append(f"{value}\n")
    text.append("\nRecent logs\n", style=heading_style)
    text.append(str(status.get("logs") or "No recent logs."))
    text.append("\n\np actions · r refresh", style="dim")
    return text


def _matches(value: str, query: str) -> bool:
    if not query:
        return True
    return query in value if any(char.isupper() for char in query) else (
        query.casefold() in value.casefold()
    )


def _auth_failure(exc: BaseException) -> bool:
    if isinstance(exc, (AuthenticationRedirect, AuthenticationRequired)):
        return True
    message = str(exc).casefold()
    return "http 401" in message or "authentication required" in message


def _load_submission_with_auth(
    cookies: tuple[str, str],
    bearer: str,
    submission_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    return load_submission(submission_id, cookies, bearer, **kwargs)


async def _finish_dom_update(update: Any) -> None:
    """Finish mounting/pruning before propagating worker cancellation.

    Textual's DOM awaitables gather child message pumps. Cancelling that gather
    can also cancel the pumps, which later breaks application shutdown.
    """
    task = asyncio.ensure_future(update)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise


async def _wait_for_play_operation(
    client: ManagerClient, operation_id: str, *, timeout: float | None,
    progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    stop_event = threading.Event()
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def wait() -> None:
        try:
            result = client.wait_operation(
                operation_id,
                timeout=timeout,
                stop_event=stop_event,
                **({"progress": lambda event: loop.call_soon_threadsafe(progress, event)} if progress else {}),
            )
        except BaseException as exc:
            callback = lambda exc=exc: (
                not future.done() and future.set_exception(exc)
            )
        else:
            callback = lambda result=result: (
                not future.done() and future.set_result(result)
            )
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:
            pass

    threading.Thread(target=wait, daemon=True).start()
    try:
        return await future
    finally:
        stop_event.set()


class LoginRequired(RuntimeError):
    """The TUI must ask the user to authenticate."""


class TUIAuthSession:
    """Own and refresh the TUI's bearer/cookie pair."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] | None = None
        self.cookies = ("", "")
        self.bearer = ""
        self.generation = 0
        self._refresh_lock = asyncio.Lock()
        if state:
            self.install(state)

    def install(self, state: dict[str, Any]) -> None:
        auth = get_auth(state)
        if not auth:
            raise LoginRequired("Saved login has no usable access token.")
        self.state = state
        self.cookies = (auth[0] or "", auth[1] or "")
        self.bearer = auth[2]
        self.generation += 1

    async def _refresh(self, failed_generation: int) -> None:
        async with self._refresh_lock:
            if self.generation != failed_generation:
                return
            if not self.state:
                raise LoginRequired("Sign in to continue.")
            try:
                refreshed = await asyncio.to_thread(
                    refresh_saved_tokens, self.state
                )
            except Exception as exc:
                raise LoginRequired("Session refresh failed. Sign in again.") from exc
            if not refreshed:
                raise LoginRequired("Session expired. Sign in again.")
            self.install(refreshed)

    async def call(
        self,
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if not self.state or not self.bearer:
            raise LoginRequired("Sign in to continue.")
        failed_generation = self.generation
        cookies, bearer = self.cookies, self.bearer
        try:
            return await asyncio.to_thread(
                function, cookies, bearer, *args, **kwargs
            )
        except Exception as exc:
            if not _auth_failure(exc):
                raise
        await self._refresh(failed_generation)
        try:
            return await asyncio.to_thread(
                function, self.cookies, self.bearer, *args, **kwargs
            )
        except Exception as exc:
            if _auth_failure(exc):
                raise LoginRequired(
                    "Authentication failed after one refresh. Sign in again."
                ) from exc
            raise


class EntityItem(ListItem):
    def __init__(self, entity: dict[str, Any], label: str | Text) -> None:
        super().__init__(Label(label if isinstance(label, Text) else Text(label)))
        self.entity = entity


class LoginScreen(ModalScreen[dict[str, Any] | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("tab", "focus_next", "Next", show=False, priority=True),
        Binding(
            "shift+tab", "focus_previous", "Previous", show=False, priority=True
        ),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="login-dialog"):
            yield Label("Sign in to Nitro Judge", classes="dialog-title")
            yield Input(placeholder="Username", id="login-username")
            yield Input(
                placeholder="Password",
                password=True,
                id="login-password",
            )
            yield Static("", id="login-error", classes="form-error", markup=False)
            yield Static(
                "Tab next · Enter sign in · Esc cancel",
                classes="dialog-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#login-username", Input).focus()

    def action_focus_next(self) -> None:
        self.focus_next()

    def action_focus_previous(self) -> None:
        self.focus_previous()

    @on(Input.Submitted)
    def input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "login-username":
            self.query_one("#login-password", Input).focus()
        else:
            self.login()

    @work(exclusive=True)
    async def login(self) -> None:
        username = self.query_one("#login-username", Input).value.strip()
        password_input = self.query_one("#login-password", Input)
        password = password_input.value
        error = self.query_one("#login-error", Static)
        if not username or not password:
            error.add_class("-error")
            error.update("[!] Username and password are required.")
            return
        error.remove_class("-error")
        error.update("Signing in…")
        try:
            result = await asyncio.to_thread(do_login, username, password)
            if not result.get("success") or not result.get("tokens"):
                raise RuntimeError(str(result.get("error") or "Login failed"))
            await asyncio.to_thread(
                save_token_state,
                result["tokens"],
                result.get("username") or username,
            )
            state = await asyncio.to_thread(load_state)
            if not state or not get_auth(state):
                raise RuntimeError("Login response did not contain usable credentials")
            password_input.value = ""
            self.dismiss(state)
        except Exception as exc:
            password_input.value = ""
            error.add_class("-error")
            error.update(f"[!] {exc}")

    def action_cancel(self) -> None:
        self.query_one("#login-password", Input).value = ""
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", "No"),
        Binding("y", "confirm", "Yes"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label("Please confirm", classes="dialog-title")
            yield Static(self.message, id="confirm-message", markup=False)
            yield OptionList("No — keep everything", "Yes — continue", id="confirm-list")
            yield Static("y yes · n/Esc no · Enter choose", classes="dialog-hint")

    def on_mount(self) -> None:
        choices = self.query_one("#confirm-list", OptionList)
        choices.highlighted = 0
        choices.focus()

    @on(OptionList.OptionSelected, "#confirm-list")
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index == 1)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass(frozen=True)
class DownloadRequest:
    categories: list[str]
    output_dir: str
    output_path: str | None


class DownloadScreen(ModalScreen[DownloadRequest | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("tab", "focus_next", "Next", show=False, priority=True),
        Binding(
            "shift+tab", "focus_previous", "Previous", show=False, priority=True
        ),
    ]

    def __init__(self, categories: list[str]) -> None:
        super().__init__()
        self.categories = categories

    def compose(self) -> ComposeResult:
        choices = [
            (category.replace("_", " ").title(), category, True)
            for category in self.categories
        ]
        with VerticalScroll(id="download-dialog"):
            yield Label("Download task data", classes="dialog-title")
            yield Label("Categories")
            yield SelectionList(*choices, id="download-categories")
            yield Label("Output directory")
            yield PathInput(value=os.getcwd(), id="download-directory", directories_only=True)
            yield Label("Single-file path (optional; one category only)")
            yield PathInput(placeholder="/path/to/file", id="download-output")
            yield Static("", id="download-error", classes="form-error", markup=False)
            yield Static(
                "Space toggle · Tab next · Enter on last field download · Esc cancel",
                classes="dialog-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#download-categories", SelectionList).focus()

    def action_focus_next(self) -> None:
        self.focus_next()

    def action_focus_previous(self) -> None:
        self.focus_previous()

    @on(Input.Submitted)
    def input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "download-directory":
            self.query_one("#download-output", Input).focus()
        else:
            self.submit()

    def submit(self) -> None:
        categories = list(
            self.query_one("#download-categories", SelectionList).selected
        )
        directory_value = self.query_one("#download-directory", Input).value.strip()
        directory = expand_path(directory_value) if directory_value else ""
        output_value = self.query_one("#download-output", Input).value.strip()
        output = expand_path(output_value) if output_value else None
        error = self.query_one("#download-error", Static)
        if not categories:
            error.update("[!] Select at least one category.")
        elif output and len(categories) != 1:
            error.update("[!] A single-file path requires exactly one category.")
        elif not directory:
            error.update("[!] Enter an output directory.")
        else:
            self.dismiss(DownloadRequest(categories, directory, output))

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True)
class SubmitRequest:
    output_path: str
    source_path: str | None
    note: str


class SubmitScreen(ModalScreen[SubmitRequest | None]):
    def __init__(self, *, source_required: bool = False) -> None:
        super().__init__()
        self.source_required = source_required

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("tab", "focus_next", "Next", show=False, priority=True),
        Binding(
            "shift+tab", "focus_previous", "Previous", show=False, priority=True
        ),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="submit-dialog"):
            yield Label("Create submission", classes="dialog-title")
            with Horizontal(classes="form-row"):
                yield Label("Output file")
                yield PathInput(placeholder="submission.csv", id="submit-output")
            with Horizontal(classes="form-row"):
                yield Label("Source file" if self.source_required else "Source (optional)")
                yield PathInput(placeholder="solution.py", id="submit-source")
            with Horizontal(classes="form-row"):
                yield Label("Note (optional)")
                yield Input(id="submit-note")
            yield Static("", id="submit-error", classes="form-error", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="submit-cancel")
                yield Button("Submit", id="submit-confirm", variant="success")
            yield Static(
                "Tab next · Enter submit · Esc cancel",
                classes="dialog-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#submit-output", Input).focus()

    def action_focus_next(self) -> None:
        self.focus_next()

    def action_focus_previous(self) -> None:
        self.focus_previous()

    @on(Input.Submitted)
    def input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "submit-output":
            self.query_one("#submit-source", Input).focus()
        elif event.input.id == "submit-source":
            self.query_one("#submit-note", Input).focus()
        else:
            self.submit()

    def submit(self) -> None:
        output = self.query_one("#submit-output", Input).value.strip()
        if not output:
            self.query_one("#submit-error", Static).update(
                "[!] Enter an output file."
            )
            return
        source = self.query_one("#submit-source", Input).value.strip()
        if self.source_required and not source:
            self.query_one("#submit-error", Static).update(
                "[!] Enter a source file for submission proxy mode."
            )
            return
        self.dismiss(
            SubmitRequest(
                expand_path(output),
                expand_path(source) if source else None,
                self.query_one("#submit-note", Input).value,
            )
        )

    @on(Button.Pressed, "#submit-confirm")
    def submit_clicked(self) -> None:
        self.submit()

    @on(Button.Pressed, "#submit-cancel")
    def cancel_clicked(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlayMenu(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self, workspace_state: str = "missing", image_state: str = "missing"
    ) -> None:
        super().__init__()
        if workspace_state == "running":
            self.actions = (
                ("open", "Open Jupyter"),
                ("stop", "Stop — preserve containers and workspace"),
                ("restart", "Restart services"),
                ("recreate", "Recreate containers"),
                ("delete-container", "Delete containers — preserve workspace"),
                ("logs", "Logs — show recent output"),
                ("dashboard", "Dashboard — open Play manager"),
            )
        elif workspace_state == "stopped":
            self.actions = (
                ("start", "Start existing services"),
                ("recreate", "Recreate containers"),
                ("delete-container", "Delete containers — preserve workspace"),
                ("logs", "Logs — show recent output"),
                ("dashboard", "Dashboard — open Play manager"),
            )
        elif workspace_state == "ready":
            self.actions = (
                ("play", "Play — prepare and start"),
                ("recreate", "Recreate containers"),
                ("delete-container", "Delete containers — preserve workspace"),
                ("logs", "Logs — show recent output"),
                ("dashboard", "Dashboard — open Play manager"),
            )
        else:
            self.actions = (
                ("play", "Play — prepare and start"),
                ("pull", "Pull competition images"),
                ("dashboard", "Dashboard — open Play manager"),
            )
        if workspace_state not in {"running", "stopped"} and image_state == "ready":
            self.actions = (
                *self.actions[:-1],
                ("delete-image", "Delete cached images — preserve workspace"),
                self.actions[-1],
            )

    def compose(self) -> ComposeResult:
        with Vertical(id="play-dialog"):
            yield Label("Play actions", classes="dialog-title")
            yield OptionList(*(label for _, label in self.actions), id="play-list")
            yield Static("j/k or arrows move · Enter run · Esc cancel", classes="dialog-hint")

    def on_mount(self) -> None:
        options = self.query_one("#play-list", OptionList)
        options.highlighted = 0
        options.focus()

    @on(OptionList.OptionSelected, "#play-list")
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.actions[event.option_index][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    def __init__(self, context: str = "") -> None:
        super().__init__()
        self.context = context

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("question_mark", "close", "Close"),
    ]
    HELP = """\
Navigate
  j/k or ↑/↓       move
  Enter             open
  h/l or ←/→        move across panes or task views
  Esc               back
  Tab/Shift+Tab     cycle panes or form fields

Global
  / filter   r refresh   ? help   q/Ctrl+D quit
  1 overview   2 data   3 submissions   4 play
  d download   s submit   p play actions

Forms
  Space toggles choices. Tab moves. Enter confirms the final field.
  Right at the end of a path accepts a filesystem suggestion.
  Destructive Play actions require confirmation; workspace deletion requires its full reference.
"""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            yield Label("Nitro AI Judge keys", classes="dialog-title")
            yield Static(self.context, markup=False)
            yield Static(self.HELP)
            yield Static("? or Esc close", classes="dialog-hint")

    def action_close(self) -> None:
        self.dismiss(None)


class NitroTUI(App[int]):
    TITLE = "Nitro AI Judge"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+d", "quit", "Quit", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("slash", "filter", "Filter"),
        Binding("r", "refresh", "Refresh"),
        Binding("f", "toggle_final", "Toggle final", show=False),
        Binding("c", "cancel_play", "Cancel operation", show=False),
        Binding("g", "toggle_logs", "Follow/pause logs", show=False),
        Binding("f3", "search_next(1)", "Next match", show=False),
        Binding("shift+f3", "search_next(-1)", "Previous match", show=False),
        Binding("enter", "open", "Open"),
        Binding("escape", "back", "Back", show=False),
        Binding("h", "left", "Left", show=False),
        Binding("left", "left", "Left", show=False),
        Binding("l", "right", "Right", show=False),
        Binding("right", "right", "Right", show=False),
        Binding("j", "down", "Down", show=False),
        Binding("down", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        Binding("up", "up", "Up", show=False),
        Binding("tab", "next_pane", "Next pane", show=False, priority=True),
        Binding(
            "shift+tab",
            "previous_pane",
            "Previous pane",
            show=False,
            priority=True,
        ),
        Binding("1", "view(1)", "Overview", show=False),
        Binding("2", "view(2)", "Data", show=False),
        Binding("3", "view(3)", "Submissions", show=False),
        Binding("4", "view(4)", "Play", show=False),
        Binding("d", "download", "Download", show=False),
        Binding("s", "submit", "Submit"),
        Binding("p", "play_menu", "Play", show=False),
    ]

    CSS = """
    Screen {
        background: $background;
        color: $text;
    }

    #header-line {
        height: 2;
        padding: 0 1;
        background: $surface;
        color: $text;
        content-align: left middle;
    }

    #main {
        height: 1fr;
    }

    #contest-pane {
        width: 26%;
        min-width: 24;
        border-right: solid $border;
        background: $background;
    }

    #task-pane {
        width: 28%;
        min-width: 24;
        border-right: solid $border;
        background: $background;
    }

    #contest-pane.-focused, #task-pane.-focused {
        border-right: heavy $accent;
    }

    #right-pane {
        width: 1fr;
        background: $background;
    }

    #contest-title, #task-title, #view-nav {
        height: 2;
        padding: 0 1;
        background: $surface;
        color: $muted;
        content-align: left middle;
        text-style: bold;
    }

    #view-nav {
        padding: 0;
    }

    #view-nav Tab.-active {
        background: $panel;
        color: $text;
        text-style: bold;
    }

    #right-pane.-focused #view-nav .underline--bar {
        background: $accent;
    }

    #contest-filter, #task-filter, #submission-filter {
        display: none;
        height: 3;
        margin: 0 1;
    }

    #contest-filter.-open, #task-filter.-open, #submission-filter.-open {
        display: block;
    }

    ListView {
        height: 1fr;
        background: $background;
    }

    ListItem {
        height: auto;
        min-height: 2;
        padding: 0 1;
        color: $text;
        border-left: tall $background;
    }

    #contest-list ListItem, #task-list ListItem {
        min-height: 3;
    }

    #contest-list Label, #task-list Label {
        width: 1fr;
        text-overflow: ellipsis;
        text-wrap: nowrap;
    }

    ListItem.-highlight {
        background: $panel;
        color: $text;
        border-left: tall $accent;
        text-style: bold;
    }

    Screen.-no-color ListItem.-highlight {
        background: $background;
        color: $text;
        border-left: tall $foreground;
        text-style: bold reverse;
    }

    ContentSwitcher, .task-view {
        height: 1fr;
    }

    .task-view {
        padding: 1 2;
        background: $background;
    }

    #overview, #data-content, #play-content {
        height: auto;
        min-height: 100%;
    }

    #overview MarkdownHeader {
        margin: 1 0 0 0;
    }

    #overview MarkdownH1 {
        content-align: left middle;
        background: transparent;
        color: $text;
        text-style: bold;
    }

    #overview MarkdownH2, #overview MarkdownH3 {
        color: $accent;
        text-style: bold;
    }

    #overview MarkdownFence {
        background: $surface;
        border-left: tall $border;
        margin: 1 0;
    }

    #submission-list {
        height: 45%;
        min-height: 4;
        border-bottom: solid $border;
    }

    #submission-actions {
        height: 3;
        align-horizontal: right;
    }

    #submission-actions .section-title {
        width: 1fr;
        padding: 0 1;
        content-align: left middle;
        text-style: bold;
    }

    #new-submission {
        min-width: 18;
    }

    #play-log-scroll { height: 12; display: none; }
    #play-log-scroll.-open { display: block; }
    #play-operation { height: auto; }

    #submission-detail-scroll {
        height: 1fr;
        padding-top: 1;
    }

    #overview-search-result { display: none; height: auto; }
    #overview-search-result.-open { display: block; }

    #overview-filter {
        display: none;
        height: 3;
        margin-bottom: 1;
    }

    #overview-filter.-open {
        display: block;
    }

    #status-line {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $muted;
    }

    #status-line.-error {
        color: $error;
        text-style: bold;
    }

    #status-line.-success {
        color: $success;
        text-style: bold;
    }

    #too-small {
        display: none;
        height: 1fr;
        content-align: center middle;
        text-align: center;
        text-style: bold;
    }

    #main.-compact #contest-pane, #main.-compact #task-pane,
    #main.-compact #right-pane {
        width: 100%;
        border: none;
    }

    #main.-contests-only #task-pane, #main.-contests-only #right-pane,
    #main.-tasks-only #contest-pane, #main.-tasks-only #right-pane,
    #main.-right-only #contest-pane, #main.-right-only #task-pane {
        display: none;
    }

    #main.-too-small #contest-pane, #main.-too-small #task-pane,
    #main.-too-small #right-pane {
        display: none;
    }

    #main.-too-small #too-small {
        display: block;
    }

    Footer {
        background: $surface;
        color: $text;
    }

    ModalScreen {
        align: center middle;
        background: $background 85%;
    }

    #login-dialog, #confirm-dialog, #help-dialog, #submit-dialog,
    #download-dialog, #play-dialog {
        width: 66;
        max-width: 92%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $surface;
        border: solid $accent;
    }

    #download-dialog {
        height: 26;
    }

    #submit-dialog {
        max-height: 100%;
        padding: 0 1;
    }

    #submit-dialog .form-row {
        height: 3;
    }

    #submit-dialog .form-row Label {
        width: 18;
        content-align: left middle;
    }

    #submit-dialog .form-row Input {
        width: 1fr;
    }

    #submit-dialog .dialog-hint {
        height: 1;
        margin-top: 0;
    }

    #download-categories {
        height: 8;
        background: $background;
    }

    #play-list, #confirm-list {
        height: auto;
        max-height: 12;
        background: $background;
    }

    OptionList > .option-list--option-highlighted,
    SelectionList > .option-list--option-highlighted {
        background: $accent;
        color: $background;
        text-style: bold reverse;
    }

    Input {
        background: $background;
        border: tall $border;
        color: $text;
    }

    Input:focus {
        border: tall $accent;
    }

    .dialog-title {
        height: 2;
        color: $accent;
        text-style: bold;
    }

    .dialog-hint {
        height: auto;
        margin-top: 1;
        color: $muted;
    }

    .form-error {
        height: auto;
        min-height: 1;
        color: $error;
        text-style: bold;
    }

    #login-error {
        height: 2;
        padding: 0 3;
        color: $muted;
        content-align: left middle;
        text-style: none;
    }

    #login-error.-error {
        color: $error;
        text-style: bold;
    }

    .dialog-actions {
        height: 3;
        align-horizontal: right;
    }

    .dialog-actions Button {
        min-width: 10;
        margin-left: 1;
    }
    """

    def __init__(self, manager_client: ManagerClient | None = None) -> None:
        super().__init__()
        self.register_theme(NAIJ_THEME)
        self.register_theme(MONO_THEME)
        self.theme = "naij-mono" if os.environ.get("NO_COLOR") is not None else "naij"
        self.session = TUIAuthSession()
        self.contests: list[dict[str, Any]] = []
        self.visible_contests: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.visible_tasks: list[dict[str, Any]] = []
        self.categories: list[str] = []
        self.submissions: list[dict[str, Any]] = []
        self.visible_submissions: list[dict[str, Any]] = []
        self.current_contest: dict[str, Any] | None = None
        self.current_task: dict[str, Any] | None = None
        self.current_submission: dict[str, Any] | None = None
        self.submission_focus = "list"
        self.active_pane = "contests"
        self.active_view = 1
        self.layout_mode = "wide"
        self.contest_generation = 0
        self.task_generation = 0
        self.submission_generation = 0
        self._login_open = False
        self.manager_client = manager_client
        self.play_snapshot: dict[str, Any] = {}
        self.play_operations: dict[tuple[str, str], dict] = {}
        self.log_lines: deque[str] = deque(maxlen=2000)
        self.logs_following = False
        self.log_generation = 0
        self.log_reference: tuple[str, str] | None = None

    def _manager(self) -> ManagerClient:
        if self.manager_client is None:
            self.manager_client = ManagerClient.from_state()
        return self.manager_client

    def compose(self) -> ComposeResult:
        yield Static("Nitro AI Judge · Contest browser", id="header-line")
        with Horizontal(id="main"):
            with Vertical(id="contest-pane"):
                yield Static("Contests", id="contest-title")
                yield Input(placeholder="Filter contests", id="contest-filter")
                yield ListView(id="contest-list")
            with Vertical(id="task-pane"):
                yield Static("Tasks", id="task-title")
                yield Input(placeholder="Filter tasks", id="task-filter")
                yield ListView(id="task-list")
            with Vertical(id="right-pane"):
                yield Tabs(
                    Tab("1 Overview", id="tab-overview"),
                    Tab("2 Data", id="tab-data"),
                    Tab("3 Submissions", id="tab-submissions"),
                    Tab("4 Play", id="tab-play"),
                    active="tab-overview",
                    id="view-nav",
                )
                with ContentSwitcher(initial="view-overview", id="task-views"):
                    with VerticalScroll(id="view-overview", classes="task-view"):
                        yield Input(placeholder="Search statement · F3 next · Shift+F3 previous · Esc close", id="overview-filter")
                        yield Static("", id="overview-search-result", markup=False)
                        yield Markdown(
                            "Select a contest to begin.",
                            id="overview",
                        )
                    with VerticalScroll(id="view-data", classes="task-view"):
                        yield Static(
                            "Select a task to inspect its data.",
                            id="data-content",
                            markup=False,
                        )
                    with Vertical(id="view-submissions", classes="task-view"):
                        with Horizontal(id="submission-actions"):
                            yield Static(
                                "Your submissions",
                                classes="section-title",
                            )
                            yield Button(
                                "New submission",
                                id="new-submission",
                                variant="success",
                            )
                            yield Button("Set final", id="submission-final", disabled=True)
                        yield Input(
                            placeholder="Filter submissions",
                            id="submission-filter",
                        )
                        yield ListView(id="submission-list")
                        with VerticalScroll(id="submission-detail-scroll"):
                            yield Static(
                                "Select a submission.",
                                id="submission-detail",
                                markup=False,
                            )
                    with VerticalScroll(id="view-play", classes="task-view"):
                        with Horizontal(classes="dialog-actions"):
                            yield Button("Cancel operation", id="play-cancel", disabled=True)
                            yield Button("Follow logs", id="play-follow")
                        yield Static("", id="play-operation", markup=False)
                        with VerticalScroll(id="play-log-scroll"):
                            yield Static("", id="play-live-logs", markup=False)
                        yield Static(
                            "Select a contest to inspect local play.",
                            id="play-content",
                            markup=False,
                        )
            yield Static(
                "Terminal too small\nResize to at least 60 × 20",
                id="too-small",
            )
        yield Static("Starting…", id="status-line", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        if os.environ.get("NO_COLOR") is not None:
            self.screen.add_class("-no-color")
        self._restore_cache()
        await self.render_left()
        self._render_view_nav()
        self._apply_layout(self.size.width, self.size.height)
        self._focus_active()
        self.authenticate()

    def on_resize(self, event: Any) -> None:
        self._apply_layout(event.size.width, event.size.height)

    def _apply_layout(self, width: int, height: int) -> None:
        main = self.query_one("#main", Horizontal)
        main.remove_class(
            "-compact",
            "-contests-only",
            "-tasks-only",
            "-right-only",
            "-too-small",
        )
        if width < 60 or height < 20:
            self.layout_mode = "too-small"
            main.add_class("-too-small")
        elif width < 100:
            self.layout_mode = "compact"
            main.add_class("-compact")
            main.add_class(
                {
                    "contests": "-contests-only",
                    "tasks": "-tasks-only",
                    "right": "-right-only",
                }[self.active_pane]
            )
        else:
            self.layout_mode = "wide"
        self.query_one("#header-line").display = self.layout_mode != "too-small"
        self.query_one("#status-line").display = self.layout_mode != "too-small"
        self.query_one(Footer).display = self.layout_mode != "too-small"
        self._mark_focus()

    def _restore_cache(self) -> None:
        self.contests = cached_items("contests", "all") or cached_items(
            "contests", "featured"
        )
        context = load_context()
        contest = selected_contest(context)
        if contest:
            self.current_contest = next(
                (item for item in self.contests if contest_ref(item) == contest),
                {
                    "organizationSlug": contest[0],
                    "competitionSlug": contest[1],
                    "title": contest[1],
                },
            )
            self.active_pane = "tasks"
            self.tasks = cached_items("tasks", f"{contest[0]}/{contest[1]}")
            task_id = selected_task(context)
            if task_id:
                self.current_task = next(
                    (
                        item
                        for item in self.tasks
                        if str(item.get("id")) == str(task_id)
                    ),
                    None,
                )
                if self.current_task:
                    self.submissions = cached_items(
                        "submissions", self.task_cache_key
                    )
                    submission_id = selected_submission(context)
                    self.current_submission = next(
                        (
                            item
                            for item in self.submissions
                            if str(item.get("id")) == str(submission_id)
                        ),
                        None,
                    )
                    self._render_task_overview(self.current_task)
        self._update_header()
        if self.contests or self.tasks:
            self.set_status("Cached data shown · connecting…")

    @property
    def task_cache_key(self) -> str:
        if not self.current_contest or not self.current_task:
            return ""
        org, comp = contest_ref(self.current_contest)
        return f"{org}/{comp}/{self.current_task.get('id')}"

    def _contest_is_current(self, generation: int, org: str, comp: str) -> bool:
        return (
            generation == self.contest_generation
            and self.current_contest is not None
            and contest_ref(self.current_contest) == (org, comp)
        )

    def _task_is_current(
        self,
        contest_generation: int,
        task_generation: int,
        org: str,
        comp: str,
        task_id: str,
    ) -> bool:
        return (
            self._contest_is_current(contest_generation, org, comp)
            and task_generation == self.task_generation
            and self.current_task is not None
            and str(self.current_task.get("id")) == task_id
        )

    def _clear_submission_context(self) -> None:
        self.workers.cancel_group(self, "submission-poll")
        self.submission_generation += 1
        self.current_submission = None
        self.submissions = []
        self.visible_submissions = []
        self.submission_focus = "list"
        self.query_one("#submission-detail", Static).update("Select a submission.")

    def _clear_task_context(self) -> None:
        self.task_generation += 1
        self.current_task = None
        self.categories = []
        self.query_one("#overview", Markdown).update("Select a task to begin.")
        self.query_one("#data-content", Static).update(
            "Select a task to inspect its data."
        )
        self._clear_submission_context()

    def set_status(self, message: str, kind: str = "") -> None:
        status = self.query_one("#status-line", Static)
        status.remove_class("-error", "-success")
        if kind:
            status.add_class(f"-{kind}")
        prefix = "[!] " if kind == "error" else ("[OK] " if kind == "success" else "")
        status.update(prefix + message)

    def _update_header(self) -> None:
        parts: list[tuple[str, str]] = []
        if self.current_contest:
            org, comp = contest_ref(self.current_contest)
            parts.append((f"{org}/{comp}", "bold"))
        if self.current_task:
            number = next(
                (
                    str(index)
                    for index, task in enumerate(self.tasks, 1)
                    if str(task.get("id")) == str(self.current_task.get("id"))
                ),
                str(self.current_task.get("id")),
            )
            parts.append(
                (f"{number}. {self.current_task.get('title') or '?'}", "bold")
            )
        if self.session.state:
            parts.append(
                (str(self.session.state.get("username") or "signed in"), "dim")
            )
        header = Text()
        header.append(
            "Nitro AI Judge",
            style="bold reverse"
            if os.environ.get("NO_COLOR") is not None
            else f"bold {ACCENT}",
        )
        for value, style in parts:
            header.append("  ›  ", style="dim")
            header.append(value, style=style)
        self.query_one("#header-line", Static).update(header)

    async def render_contests(self) -> None:
        contest_query = self.query_one("#contest-filter", Input).value
        self.visible_contests = [
            item
            for item in self.contests
            if _matches(
                f"{item.get('title', '')} {'/'.join(contest_ref(item))}",
                contest_query,
            )
        ]
        self.query_one("#contest-title", Static).update(
            f"Contests  {len(self.visible_contests)}/{len(self.contests)}"
        )
        contest_view = self.query_one("#contest-list", ListView)
        await _finish_dom_update(contest_view.clear())
        if self.visible_contests:
            await _finish_dom_update(contest_view.extend(
                EntityItem(item, contest_label(item))
                for item in self.visible_contests
            ))
            selected_ref = (
                contest_ref(self.current_contest) if self.current_contest else None
            )
            contest_view.index = next(
                (
                    index
                    for index, item in enumerate(self.visible_contests)
                    if contest_ref(item) == selected_ref
                ),
                0,
            )

    async def render_tasks(self) -> None:
        task_query = self.query_one("#task-filter", Input).value
        numbered = list(enumerate(self.tasks, 1))
        visible_numbered = [
            (number, item)
            for number, item in numbered
            if _matches(
                f"{number} {item.get('title', '')} {item.get('synopsis', '')}",
                task_query,
            )
        ]
        self.visible_tasks = [item for _, item in visible_numbered]
        self.query_one("#task-title", Static).update(
            f"Tasks  {len(self.visible_tasks)}/{len(self.tasks)}"
        )
        task_view = self.query_one("#task-list", ListView)
        await _finish_dom_update(task_view.clear())
        if self.visible_tasks:
            await _finish_dom_update(task_view.extend(
                EntityItem(item, task_label(item, number))
                for number, item in visible_numbered
            ))
            selected_id = (
                str(self.current_task.get("id")) if self.current_task else None
            )
            task_view.index = next(
                (
                    index
                    for index, item in enumerate(self.visible_tasks)
                    if str(item.get("id")) == selected_id
                ),
                0,
            )

    async def render_left(self) -> None:
        await self.render_contests()
        await self.render_tasks()

    @on(Input.Changed, "#contest-filter")
    def contest_filter_changed(self) -> None:
        self.render_contests_worker()

    @on(Input.Changed, "#task-filter")
    def task_filter_changed(self) -> None:
        self.render_tasks_worker()

    @work(group="left-render", exclusive=True)
    async def render_left_worker(self) -> None:
        await self.render_left()

    @work(group="contest-render", exclusive=True)
    async def render_contests_worker(self) -> None:
        await self.render_contests()

    @work(group="task-render", exclusive=True)
    async def render_tasks_worker(self) -> None:
        await self.render_tasks()

    @on(Input.Submitted, "#contest-filter")
    def contest_filter_submitted(self) -> None:
        self.query_one("#contest-filter", Input).remove_class("-open")
        self.query_one("#contest-list", ListView).focus()

    @on(Input.Submitted, "#task-filter")
    def task_filter_submitted(self) -> None:
        self.query_one("#task-filter", Input).remove_class("-open")
        self.query_one("#task-list", ListView).focus()

    @on(ListView.Highlighted, "#contest-list")
    def contest_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, EntityItem) and self.active_pane == "contests":
            self._render_context_preview(event.item.entity, "contests")

    @on(ListView.Highlighted, "#task-list")
    def task_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, EntityItem) and self.active_pane == "tasks":
            self._render_context_preview(event.item.entity, "tasks")

    @on(ListView.Selected, "#contest-list")
    def contest_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, EntityItem):
            return
        self.open_contest(event.item.entity)

    @on(ListView.Selected, "#task-list")
    def task_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, EntityItem):
            return
        self.open_task(event.item.entity)

    def _render_context_preview(
        self, entity: dict[str, Any], pane: str
    ) -> None:
        if pane == "contests":
            org, comp = contest_ref(entity)
            title = entity.get("title") or comp
            start = format_datetime_ms(entity.get("competitionStart"))
            end = format_datetime_ms(entity.get("competitionEnd"))
            text = f"# {title}\n\n`{org}/{comp}`"
            if entity.get("competitionStart") or entity.get("competitionEnd"):
                text += f"\n\n{start} → {end}"
            text += "\n\nPress Enter to open tasks."
        else:
            number = next(
                (
                    index
                    for index, item in enumerate(self.tasks, 1)
                    if item is entity
                ),
                "?",
            )
            text = (
                f"# {number}. {entity.get('title') or '?'}\n\n"
                f"{entity.get('synopsis') or 'No synopsis available.'}\n\n"
                "Press Enter to open the task."
            )
        self.query_one("#overview", Markdown).update(text)

    def open_contest(self, contest: dict[str, Any]) -> None:
        self._pause_logs()
        self.query_one("#play-log-scroll", VerticalScroll).remove_class("-open")
        self.query_one("#play-operation", Static).update("")
        self.contest_generation += 1
        self._clear_task_context()
        self.play_snapshot = {}
        self.query_one("#play-content", Static).update(
            "Select Play to load this contest's local state."
        )
        self.current_contest = contest
        self.active_pane = "tasks"
        org, comp = contest_ref(contest)
        set_contest(contest)
        self.tasks = cached_items("tasks", f"{org}/{comp}")
        self.query_one("#task-filter", Input).value = ""
        self.render_tasks_worker()
        self._update_header()
        self._apply_layout(self.size.width, self.size.height)
        self._focus_active()
        self.set_status("Cached tasks shown · refreshing…")
        self.refresh_tasks()

    def open_task(self, task: dict[str, Any]) -> None:
        self.task_generation += 1
        self._clear_submission_context()
        self.categories = []
        self.query_one("#data-content", Static).update("Loading task data…")
        self.current_task = task
        set_task(task)
        self.submissions = cached_items("submissions", self.task_cache_key)
        self.active_view = 1
        self.active_pane = "right"
        self._render_task_overview(task)
        self._show_active_view()
        self._update_header()
        self._apply_layout(self.size.width, self.size.height)
        self._focus_active()
        self.set_status("Cached task shown · refreshing…")
        self.refresh_task()
        self.refresh_categories()

    def _render_task_overview(self, task: dict[str, Any]) -> None:
        title = str(task.get("title") or "?")
        statement = str(
            task.get("statement")
            or task.get("synopsis")
            or "No statement is available."
        ).strip()
        markdown = statement if statement.startswith("#") else f"# {title}\n\n{statement}"
        self.overview_source = markdown
        self.query_one("#overview", Markdown).update(markdown)
        self._update_overview_search()

    def _render_view_nav(self) -> None:
        self._refresh_context_bindings()
        final = self.query_one("#submission-final", Button)
        final.disabled = not bool((self.current_submission or {}).get("id"))
        final.label = "Unset final" if (self.current_submission or {}).get("isFinal") else "Set final"
        tabs = self.query_one("#view-nav", Tabs)
        tabs.active = (
            "tab-overview",
            "tab-data",
            "tab-submissions",
            "tab-play",
        )[self.active_view - 1]
        count = (
            f" · {len(self.visible_submissions)}/{len(self.submissions)}"
            if self.active_view == 3
            else ""
        )
        tabs.get_tab("tab-submissions").label = f"3 Submissions{count}"

    @on(Tabs.TabActivated, "#view-nav")
    def view_tab_activated(self, event: Tabs.TabActivated) -> None:
        number = {
            "tab-overview": 1,
            "tab-data": 2,
            "tab-submissions": 3,
            "tab-play": 4,
        }.get(event.tab.id)
        if number is not None and number != self.active_view:
            self.action_view(number)

    def _show_active_view(self) -> None:
        if not self.query("#task-views"):
            return
        ids = ("view-overview", "view-data", "view-submissions", "view-play")
        self.query_one("#task-views", ContentSwitcher).current = ids[
            self.active_view - 1
        ]
        self._render_view_nav()
        if self.active_view != 3:
            self.workers.cancel_group(self, "submission-poll")
        if self.active_view != 4:
            self._pause_logs()
        elif self.current_contest:
            self._render_play_operation()
        if self.active_view == 3:
            self._resume_pending_submission_poll_current()
            self.render_submissions_worker()
            if self.current_task and self.session.state:
                self.refresh_submissions()
        elif self.active_view == 4 and self.current_contest:
            self.refresh_play_status()

    @work(group="authenticate", exclusive=True)
    async def authenticate(self) -> None:
        try:
            state = await asyncio.to_thread(load_state)
            if not state:
                self.require_login("Sign in to load live contest data.")
                return
            fresh = await asyncio.to_thread(ensure_fresh_state, state)
            if not fresh:
                self.require_login("Saved login expired.")
                return
            self.session.install(fresh)
            self._update_header()
            self.set_status("Connected · refreshing…")
            if self.current_contest:
                self.refresh_tasks()
                if self.current_task:
                    self.refresh_task()
                    self.refresh_categories()
            else:
                self.refresh_contests()
        except (CredentialsError, LoginRequired) as exc:
            self.require_login(str(exc))
        except Exception as exc:
            self.set_status(f"Could not restore login: {exc}", "error")
            self.require_login("Sign in to reconnect.")

    def require_login(self, message: str) -> None:
        self.workers.cancel_group(self, "submission-poll")
        self.set_status(f"{message} Cached data remains available.", "error")
        if not self._login_open:
            self._login_open = True
            self.login_flow()

    @work(group="login-flow", exclusive=True)
    async def login_flow(self) -> None:
        try:
            state = await self.push_screen_wait(LoginScreen())
            if not state:
                self.set_status("Offline · cached data remains available.")
                return
            self.session.install(state)
            self._update_header()
            self.set_status("Signed in · refreshing…", "success")
            if self.current_contest:
                self.refresh_tasks()
                if self.current_task:
                    self.refresh_task()
                    self.refresh_categories()
            else:
                self.refresh_contests()
        finally:
            self._login_open = False

    def _network_error(self, context: str, exc: BaseException) -> None:
        if isinstance(exc, LoginRequired) or _auth_failure(exc):
            self.require_login(f"{context}: authentication required.")
        else:
            self.set_status(
                f"{context}: {exc}. Cached data remains available.",
                "error",
            )

    @work(group="contests", exclusive=True)
    async def refresh_contests(self) -> None:
        self.set_status("Loading contests…")
        try:
            contests = await self.session.call(
                load_competitions,
                page=None,
                page_size=TUI_CONTEST_PAGE_SIZE,
                featured=None,
                all_pages=True,
            )
            self.contests = contests
            update_cache("contests", "all", contests)
            await self.render_contests()
            self.set_status(f"{len(contests)} contests", "success")
        except Exception as exc:
            self._network_error("Could not load contests", exc)

    @work(group="tasks", exclusive=True)
    async def refresh_tasks(self) -> None:
        if not self.current_contest:
            return
        generation = self.contest_generation
        org, comp = contest_ref(self.current_contest)
        self.set_status("Loading tasks…")
        try:
            tasks = await self.session.call(load_tasks, org, comp)
            if not self._contest_is_current(generation, org, comp):
                return
            statements = {
                str(item.get("id")): item.get("statement")
                for item in self.tasks
                if item.get("statement")
            }
            tasks = [
                (
                    {**task, "statement": statements[str(task.get("id"))]}
                    if str(task.get("id")) in statements
                    and not task.get("statement")
                    else task
                )
                for task in tasks
            ]
            self.tasks = tasks
            update_cache("tasks", f"{org}/{comp}", tasks)
            await self.render_tasks()
            self.set_status(f"{len(tasks)} tasks", "success")
        except Exception as exc:
            if self._contest_is_current(generation, org, comp):
                self._network_error("Could not load tasks", exc)

    @work(group="task", exclusive=True)
    async def refresh_task(self) -> None:
        if not self.current_contest or not self.current_task:
            return
        contest_generation = self.contest_generation
        task_generation = self.task_generation
        org, comp = contest_ref(self.current_contest)
        task_id = str(self.current_task.get("id"))
        try:
            payload = await self.session.call(
                load_task_view, org, comp, task_id
            )
            if not self._task_is_current(
                contest_generation, task_generation, org, comp, task_id
            ):
                return
            task = payload["task"]
            loaded_task_id = str(task.get("id"))
            existing = next(
                (
                    item
                    for item in self.tasks
                    if str(item.get("id")) == loaded_task_id
                ),
                {},
            )
            task = {**existing, **task}
            found = any(
                str(item.get("id")) == loaded_task_id for item in self.tasks
            )
            self.tasks = [
                task if str(item.get("id")) == loaded_task_id else item
                for item in self.tasks
            ]
            if not found:
                self.tasks.append(task)
            update_cache("tasks", f"{org}/{comp}", self.tasks)
            self.current_task = task
            set_task(task)
            self._render_task_overview(task)
            self._update_header()
            self.set_status("Task loaded", "success")
        except Exception as exc:
            if self._task_is_current(
                contest_generation, task_generation, org, comp, task_id
            ):
                self._network_error("Could not load task", exc)

    @work(group="categories", exclusive=True)
    async def refresh_categories(self) -> None:
        if not self.current_contest or not self.current_task:
            return
        contest_generation = self.contest_generation
        task_generation = self.task_generation
        org, comp = contest_ref(self.current_contest)
        task_id = str(self.current_task.get("id"))
        try:
            categories = await self.session.call(
                load_task_file_categories, org, comp, task_id
            )
            if not self._task_is_current(
                contest_generation, task_generation, org, comp, task_id
            ):
                return
            self.categories = categories
            lines = ["Available task data", ""]
            lines.extend(
                f"• {category.replace('_', ' ').title()}"
                for category in categories
            )
            if not categories:
                lines.append("No downloadable data was found.")
            lines.extend(("", "Press d to choose files and a destination."))
            self.query_one("#data-content", Static).update("\n".join(lines))
        except Exception as exc:
            if self._task_is_current(
                contest_generation, task_generation, org, comp, task_id
            ):
                self._network_error("Could not load task data", exc)

    @on(Input.Changed, "#submission-filter")
    def submission_filter_changed(self) -> None:
        self.render_submissions_worker()

    def _update_overview_search(self) -> None:
        fields = self.query("#overview-filter")
        results = self.query("#overview-search-result")
        if not fields or not results:
            return
        field = fields.first(Input)
        source = getattr(self, "overview_source", "") or str((self.current_task or {}).get("statement") or "")
        query = field.value
        result = results.first(Static)
        active = bool(query and field.has_class("-open"))
        result.set_class(active, "-open")
        self.query_one("#overview", Markdown).display = not active
        self.overview_matches = list(re.finditer(re.escape(query), source, re.IGNORECASE)) if query else []
        if not active:
            return
        count = len(self.overview_matches)
        self.overview_match_index = getattr(self, "overview_match_index", 0) % max(1, count)
        text = Text(source)
        for index, match in enumerate(self.overview_matches):
            text.stylize("bold reverse" if index == self.overview_match_index else "underline", match.start(), match.end())
        result.update(text)
        if self.query("#status-line"):
            self.set_status(f"Match {self.overview_match_index + 1 if count else 0}/{count} · F3 next · Shift+F3 previous · Esc closes")
        if count:
            self.call_after_refresh(self._scroll_overview_match)

    def _scroll_overview_match(self) -> None:
        from textual.geometry import Region
        if not getattr(self, "overview_matches", []):
            return
        match = self.overview_matches[self.overview_match_index]
        result = self.query_one("#overview-search-result", Static)
        prefix = Text(self.overview_source[:match.start()])
        row = max(0, len(prefix.wrap(self.console, max(1, result.content_region.width))) - 1)
        self.query_one("#view-overview", VerticalScroll).scroll_to_region(
            Region(0, result.virtual_region.y + row, 1, 1), animate=False, top=True, immediate=True,
        )

    def action_search_next(self, direction: int) -> None:
        if self.active_view == 1 and self.query_one("#overview-filter", Input).has_class("-open"):
            self.overview_match_index = getattr(self, "overview_match_index", 0) + direction
            self._update_overview_search()

    @on(Input.Changed, "#overview-filter")
    def overview_filter_changed(self) -> None:
        self.overview_match_index = 0
        self._update_overview_search()

    @on(Input.Submitted, "#overview-filter")
    def overview_filter_submitted(self) -> None:
        self.query_one("#view-overview", VerticalScroll).focus()

    @on(Input.Submitted, "#submission-filter")
    def submission_filter_submitted(self) -> None:
        self.query_one("#submission-filter", Input).remove_class("-open")
        self.query_one("#submission-list", ListView).focus()

    @on(Button.Pressed, "#new-submission")
    def new_submission_clicked(self) -> None:
        self.action_submit()

    @on(Button.Pressed, "#submission-final")
    def final_clicked(self) -> None:
        self.action_toggle_final()

    def action_toggle_final(self) -> None:
        if self.active_view == 3 and self.current_submission:
            self.final_selection_flow(not bool(self.current_submission.get("isFinal")))

    @work(group="submission-final", exclusive=True)
    async def final_selection_flow(self, final: bool) -> None:
        if not self.current_submission or not self.current_contest or not self.current_task:
            self.set_status("Select a submission first.", "error")
            return
        if not self.session.state:
            self.require_login("Sign in to change final selection.")
            return
        submission_id = str(self.current_submission.get("id") or "")
        if not submission_id:
            self.set_status("Selected submission has no ID.", "error")
            return
        org, comp = contest_ref(self.current_contest)
        task_id = str(self.current_task.get("id"))
        generation = self.submission_generation
        contest_generation, task_generation = self.contest_generation, self.task_generation

        def current() -> bool:
            return (
                self._task_is_current(contest_generation, task_generation, org, comp, task_id)
                and generation == self.submission_generation
                and str((self.current_submission or {}).get("id")) == submission_id
            )

        confirmed = await self.push_screen_wait(ConfirmScreen(
            f"{'Set' if final else 'Unset'} submission {submission_id} as final? This changes your final selection."
        ))
        if not confirmed or not current():
            return
        try:
            await self.session.call(set_submission_final, submission_id, final)
            if not current():
                return
            self.current_submission = {**self.current_submission, "isFinal": final}
            self.submissions = [
                {**item, "isFinal": final} if str(item.get("id")) == submission_id else item
                for item in self.submissions
            ]
            update_cache("submissions", self.task_cache_key, self.submissions)
            set_submission(self.current_submission)
            self.query_one("#submission-detail", Static).update(
                submission_details(self.current_submission)
            )
            self.set_status(
                f"Submission {submission_id} {'set as' if final else 'unset as'} final.",
                "success",
            )
            self._render_view_nav()
            self.refresh_submissions()
        except Exception as exc:
            if current():
                self._network_error("Could not update final submission", exc)

    async def render_submissions(self) -> None:
        query = self.query_one("#submission-filter", Input).value
        username = str(
            (self.session.state or {}).get("username") or ""
        ).casefold()
        owned = [
            item
            for item in self.submissions
            if not username
            or str(item.get("username") or item.get("author") or "").casefold()
            == username
        ]
        self.submissions = owned
        visible = [
            item
            for item in owned
            if _matches(
                " ".join(
                    str(item.get(key) or "")
                    for key in ("id", "username", "state", "note", "verdictMessage")
                ),
                query,
            )
        ]
        self.visible_submissions = visible
        view = self.query_one("#submission-list", ListView)
        await _finish_dom_update(view.clear())
        selected_id = str((self.current_submission or {}).get("id") or "")
        selected_mode = (self.current_submission or {}).get("_mode")
        if visible:
            await _finish_dom_update(view.extend(
                EntityItem(item, submission_label(item)) for item in visible
            ))
            index = next(
                (
                    i
                    for i, item in enumerate(visible)
                    if str(item.get("id") or "") == selected_id
                    and (selected_mode is None or item.get("_mode") == selected_mode)
                ),
                0,
            )
            view.index = index
            row = visible[index]
            if (
                str(row.get("id") or "") == selected_id and self.current_submission
                and (selected_mode is None or row.get("_mode") == selected_mode)
            ):
                self.current_submission = {**row, **self.current_submission}
            else:
                self.submission_generation += 1
                self.current_submission = row
            set_submission(self.current_submission)
            self.query_one("#submission-detail", Static).update(
                submission_details(self.current_submission)
            )
            self._resume_pending_submission_poll_current()
        elif query or not self.current_submission:
            self.query_one("#submission-detail", Static).update("Select a submission.")
        self._render_view_nav()

    @work(group="submission-render", exclusive=True)
    async def render_submissions_worker(self) -> None:
        await self.render_submissions()

    @on(ListView.Selected, "#submission-list")
    def submission_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, EntityItem):
            return
        self.current_submission = event.item.entity
        set_submission(event.item.entity)
        self.query_one("#submission-detail", Static).update(
            submission_details(event.item.entity)
        )
        self.submission_generation += 1
        self._render_view_nav()
        self.refresh_submission_details()
        self._resume_pending_submission_poll_current()

    @work(group="submissions", exclusive=True)
    async def refresh_submissions(self) -> None:
        if not self.current_contest or not self.current_task:
            return
        username = str(
            (self.session.state or {}).get("username") or ""
        ).strip()
        if not username:
            self.set_status(
                "Could not determine the signed-in user.",
                "error",
            )
            return
        contest_generation = self.contest_generation
        task_generation = self.task_generation
        org, comp = contest_ref(self.current_contest)
        task_id = str(self.current_task.get("id"))
        self.set_status("Loading submissions…")
        try:
            partial, complete = await asyncio.gather(
                self.session.call(
                    load_submissions,
                    org,
                    comp,
                    task_id,
                    author=username,
                    page=None,
                    page_size=DEFAULT_SUBMISSION_PAGE_SIZE,
                    mode="partial",
                ),
                self.session.call(
                    load_submissions,
                    org,
                    comp,
                    task_id,
                    author=username,
                    page=None,
                    page_size=DEFAULT_SUBMISSION_PAGE_SIZE,
                    mode="complete",
                ),
            )
            if not self._task_is_current(
                contest_generation, task_generation, org, comp, task_id
            ):
                return
            self.submissions = [
                *({**item, "_mode": "partial"} for item in partial[0]),
                *({**item, "_mode": "complete"} for item in complete[0]),
            ]
            update_cache("submissions", self.task_cache_key, self.submissions)
            await self.render_submissions()
            self.set_status(f"{len(self.submissions)} submissions", "success")
        except Exception as exc:
            if self._task_is_current(
                contest_generation, task_generation, org, comp, task_id
            ):
                self._network_error("Could not load submissions", exc)

    @work(group="submission-detail", exclusive=True)
    async def refresh_submission_details(self) -> None:
        if (
            not self.current_contest
            or not self.current_task
            or not self.current_submission
        ):
            return
        contest_generation = self.contest_generation
        task_generation = self.task_generation
        submission_generation = self.submission_generation
        org, comp = contest_ref(self.current_contest)
        task_id = str(self.current_task.get("id"))
        submission_id = str(self.current_submission.get("id"))
        try:
            submission = await self.session.call(
                _load_submission_with_auth,
                submission_id,
                org=org,
                comp=comp,
                task_id=task_id,
            )
            if (
                not self._task_is_current(
                    contest_generation, task_generation, org, comp, task_id
                )
                or submission_generation != self.submission_generation
                or not self.current_submission
                or str(self.current_submission.get("id")) != submission_id
            ):
                return
            if self.current_submission.get("_mode"):
                submission = {**submission, "_mode": self.current_submission["_mode"]}
            self.current_submission = submission
            set_submission(submission)
            self._render_view_nav()
            self.query_one("#submission-detail", Static).update(
                submission_details(submission)
            )
            self._resume_pending_submission_poll(
                submission_id,
                org,
                comp,
                task_id,
                contest_generation,
                task_generation,
                submission_generation,
            )
        except Exception as exc:
            if (
                self._task_is_current(
                    contest_generation, task_generation, org, comp, task_id
                )
                and submission_generation == self.submission_generation
                and self.current_submission
                and str(self.current_submission.get("id")) == submission_id
            ):
                self._network_error("Could not load submission", exc)

    @work(group="play-status", exclusive=True)
    async def refresh_play_status(self) -> None:
        if not self.current_contest:
            return
        generation = self.contest_generation
        org, comp = contest_ref(self.current_contest)
        try:
            client = self._manager()
            snapshot, log_value = await asyncio.gather(
                asyncio.to_thread(client.competition, org, comp),
                asyncio.to_thread(client.logs, org, comp, tail=80),
            )
            if not self._contest_is_current(generation, org, comp):
                return
            self.play_snapshot = snapshot
            if isinstance(snapshot.get("operation"), dict):
                self.play_operations[(org, comp)] = snapshot["operation"]
            self._render_play_operation()
            status = {
                **snapshot,
                "state": snapshot.get("workspace_state") or "missing",
                "manager_url": f"{client.base_url}/nitro/",
                "jupyter_url": (
                    f"{client.base_url}{snapshot['jupyter_url']}"
                    if snapshot.get("jupyter_url")
                    else None
                ),
                "proxy_url": (
                    f"{client.base_url}{snapshot['proxy_url']}"
                    if snapshot.get("proxy_url")
                    else None
                ),
                "gpu": snapshot.get("gpu") or "manager-selected",
                "images": ", ".join(
                    str(item.get("name"))
                    for item in (snapshot.get("images") or {}).values()
                    if isinstance(item, dict) and item.get("name")
                ),
                "logs": log_value.get("logs") or "",
            }
            self.query_one("#play-content", Static).update(play_details(status))
            self.set_status(f"Play: {status.get('state') or 'unknown'}", "success")
        except Exception as exc:
            if not self._contest_is_current(generation, org, comp):
                return
            self.play_snapshot = {}
            self.query_one("#play-content", Static).update(
                "PLAY MANAGER UNAVAILABLE\n\n"
                f"{exc}\n\nInstall or repair with:\n"
                "naij play manager install --yes\n\n"
                "Stable dashboard: http://localhost:51123/nitro/"
            )
            self.set_status(f"Could not load Play status: {exc}", "error")

    def action_view(self, number: int) -> None:
        if number < 1 or number > 4:
            return
        self.active_view = number
        self.active_pane = "right"
        if number == 1 and self.current_task:
            self._render_task_overview(self.current_task)
        self._show_active_view()
        self._apply_layout(self.size.width, self.size.height)
        self._focus_active()

    def action_filter(self) -> None:
        if self.active_pane == "right" and self.active_view == 1:
            self.overview_original_scroll = self.query_one("#view-overview", VerticalScroll).scroll_y
            field = self.query_one("#overview-filter", Input)
        elif self.active_pane == "right" and self.active_view == 3:
            field = self.query_one("#submission-filter", Input)
        elif self.active_pane == "contests":
            field = self.query_one("#contest-filter", Input)
        else:
            self.active_pane = "tasks"
            field = self.query_one("#task-filter", Input)
        field.add_class("-open")
        field.focus()
        self._apply_layout(self.size.width, self.size.height)

    def action_open(self) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            return
        if self.active_pane == "contests":
            view = self.query_one("#contest-list", ListView)
            if view.index is None or view.index >= len(self.visible_contests):
                return
            self.open_contest(self.visible_contests[view.index])
        elif self.active_pane == "tasks":
            view = self.query_one("#task-list", ListView)
            if view.index is None or view.index >= len(self.visible_tasks):
                return
            self.open_task(self.visible_tasks[view.index])
        elif self.active_view == 3:
            view = self.query_one("#submission-list", ListView)
            if view.index is not None and view.index < len(self.visible_submissions):
                self.current_submission = self.visible_submissions[view.index]
                set_submission(self.current_submission)
                self.query_one("#submission-detail", Static).update(
                    submission_details(self.current_submission)
                )
                self.submission_generation += 1
                self._render_view_nav()
                self.refresh_submission_details()
                self._resume_pending_submission_poll_current()
        elif self.active_view == 4:
            self.action_play_menu()

    def action_back(self) -> None:
        field = self.query_one("#overview-filter", Input)
        if self.active_view == 1 and field.has_class("-open"):
            field.remove_class("-open")
            field.value = ""
            self._update_overview_search()
            viewport = self.query_one("#view-overview", VerticalScroll)
            viewport.focus()
            self.call_after_refresh(viewport.scroll_to, y=getattr(self, "overview_original_scroll", 0), animate=False)
            return
        focused = self.focused
        if isinstance(focused, Input):
            focused.value = ""
            focused.remove_class("-open")
            if focused.id == "overview-filter":
                if self.current_task:
                    self._render_task_overview(self.current_task)
                self.query_one("#view-overview", VerticalScroll).focus()
            elif focused.id == "submission-filter":
                self.query_one("#submission-list", ListView).focus()
            elif focused.id == "contest-filter":
                self.query_one("#contest-list", ListView).focus()
            else:
                self.query_one("#task-list", ListView).focus()
            return
        if self.active_pane == "right":
            self.active_pane = "tasks"
        elif self.active_pane == "tasks":
            self.active_pane = "contests"
        self._apply_layout(self.size.width, self.size.height)
        self._focus_active()

    def action_left(self) -> None:
        if isinstance(self.focused, Input):
            return
        if self.active_pane == "right" and self.active_view > 1:
            self.action_view(self.active_view - 1)
            return
        if self.active_pane == "right":
            self.active_pane = "tasks"
        elif self.active_pane == "tasks":
            self.active_pane = "contests"
        self._apply_layout(self.size.width, self.size.height)
        self._focus_active()

    def action_right(self) -> None:
        if isinstance(self.focused, Input):
            return
        if self.active_pane == "contests":
            self.active_pane = "tasks"
            self._apply_layout(self.size.width, self.size.height)
            self._focus_active()
        elif self.active_pane == "tasks":
            self.active_pane = "right"
            self._apply_layout(self.size.width, self.size.height)
            self._focus_active()
        elif self.active_view < 4:
            self.action_view(self.active_view + 1)

    def action_down(self) -> None:
        view = self.focused
        if isinstance(view, (ListView, OptionList, SelectionList)):
            view.action_cursor_down()
        elif isinstance(view, VerticalScroll):
            view.scroll_down(immediate=True)

    def action_up(self) -> None:
        view = self.focused
        if isinstance(view, (ListView, OptionList, SelectionList)):
            view.action_cursor_up()
        elif isinstance(view, VerticalScroll):
            view.scroll_up(immediate=True)

    def action_next_pane(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_next()
            return
        if self.active_pane == "right" and self.active_view == 3:
            self._toggle_submission_focus()
            return
        panes = ("contests", "tasks", "right")
        self.active_pane = panes[(panes.index(self.active_pane) + 1) % len(panes)]
        if (
            self.active_pane == "right"
            and self.active_view == 1
            and self.current_task
        ):
            self._render_task_overview(self.current_task)
        self._apply_layout(self.size.width, self.size.height)
        self._focus_active()

    def action_previous_pane(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_previous()
            return
        if self.active_pane == "right" and self.active_view == 3:
            self._toggle_submission_focus()
            return
        panes = ("contests", "tasks", "right")
        self.active_pane = panes[(panes.index(self.active_pane) - 1) % len(panes)]
        if (
            self.active_pane == "right"
            and self.active_view == 1
            and self.current_task
        ):
            self._render_task_overview(self.current_task)
        self._apply_layout(self.size.width, self.size.height)
        self._focus_active()


    def _focus_submission_list(self) -> None:
        self.submission_focus = "list"
        self.query_one("#submission-list", ListView).focus()

    def _focus_submission_detail(self) -> None:
        self.submission_focus = "detail"
        self.query_one("#submission-detail-scroll", VerticalScroll).focus()

    def _toggle_submission_focus(self) -> None:
        if self.submission_focus == "detail":
            self._focus_submission_list()
        else:
            self._focus_submission_detail()
    def _focus_active(self) -> None:
        self._refresh_context_bindings()
        if self.layout_mode == "too-small":
            return
        if self.active_pane == "contests":
            self.query_one("#contest-list", ListView).focus()
        elif self.active_pane == "tasks":
            self.query_one("#task-list", ListView).focus()
        elif self.active_view == 3:
            if self.submission_focus == "detail" and self.current_submission is not None:
                self._focus_submission_detail()
            else:
                self._focus_submission_list()
        else:
            self.query_one(
                f"#view-{('overview', 'data', 'submissions', 'play')[self.active_view - 1]}"
            ).focus()
        self._mark_focus()

    def _mark_focus(self) -> None:
        if not self.is_mounted:
            return
        self.query_one("#contest-pane").set_class(
            self.active_pane == "contests", "-focused"
        )
        self.query_one("#task-pane").set_class(
            self.active_pane == "tasks", "-focused"
        )
        self.query_one("#right-pane").set_class(
            self.active_pane == "right", "-focused"
        )

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        ids = {
            widget.id
            for widget in event.widget.ancestors_with_self
            if widget.id is not None
        }
        pane = next(
            (
                name
                for name, pane_id in (
                    ("contests", "contest-pane"),
                    ("tasks", "task-pane"),
                    ("right", "right-pane"),
                )
                if pane_id in ids
            ),
            None,
        )
        if pane is not None:
            self.active_pane = pane
            self._mark_focus()

    def action_refresh(self) -> None:
        if not self.session.state:
            self.require_login("Sign in to refresh.")
        elif self.active_pane == "contests":
            self.refresh_contests()
        elif self.active_pane == "tasks":
            self.refresh_tasks()
        elif self.active_view == 3:
            self.refresh_submissions()
        elif self.active_view == 4:
            self.refresh_play_status()
        elif self.current_task:
            self.refresh_task()
            if self.active_view == 2:
                self.refresh_categories()

    def context_actions(self) -> list[str]:
        if self.active_pane in {"contests", "tasks"}:
            return ["filter", "open", "refresh", "help", "quit"]
        if self.active_view == 1:
            return ["filter", "download", "refresh", "help", "quit"] if self.current_task else ["help", "quit"]
        if self.active_view == 2:
            return ["download", "refresh", "help", "quit"] if self.current_task else ["help", "quit"]
        if self.active_view == 3:
            return (["toggle_final"] if self.current_submission else []) + ["submit", "filter", "help", "quit"]
        operation = self.play_operations.get(contest_ref(self.current_contest), {}) if self.current_contest else {}
        return (["cancel_play"] if operation.get("status") in {"queued", "running"} else ["refresh"]) + ["toggle_logs", "play_menu", "help", "quit"]

    def _refresh_context_bindings(self) -> None:
        visible = set(self.context_actions())
        primary_keys = {binding.key for binding in self.BINDINGS if binding.action in visible and binding.key != "ctrl+d"}
        # Only presentation changes: hidden bindings still work in other panes.
        for key, bindings in self._bindings.key_to_bindings.items():
            self._bindings.key_to_bindings[key] = [replace(binding, show=binding.action in visible and key in primary_keys) for binding in bindings]
        self.refresh_bindings()

    def action_help(self) -> None:
        if self.active_pane == "contests":
            context = "Contests: / filters contests; Enter opens the highlighted competition."
        elif self.active_pane == "tasks":
            context = "Tasks: / filters tasks; Enter opens the highlighted task."
        else:
            context = (
                "Overview: / searches locally; F3/Shift+F3 move matches; Esc restores the statement.",
                "Data: d opens downloads; Space selects categories in the form.",
                "Submissions: / filters rows; Tab toggles list/detail; j/k scroll feedback; f toggles final selection with confirmation.",
                "Play: p actions; c cancels the displayed active operation; g follows/pauses logs; r refreshes status.",
            )[self.active_view - 1]
        hints = [f"{binding.key}: {binding.description}" for binding in self.BINDINGS if binding.action in self.context_actions() and binding.key != "ctrl+d"]
        self.push_screen(HelpScreen(context + "\n" + " · ".join(hints)))

    def action_download(self) -> None:
        if not self.current_task:
            self.set_status("Select a task before downloading.", "error")
        elif not self.session.state:
            self.require_login("Sign in before downloading.")
        elif not self.categories:
            self.set_status("No downloadable categories are available.", "error")
        else:
            self.download_flow()

    @work(group="download-flow", exclusive=True)
    async def download_flow(self) -> None:
        request = await self.push_screen_wait(DownloadScreen(self.categories))
        if request:
            await self.perform_download(request)

    async def perform_download(
        self, request: DownloadRequest, *, force: bool = False
    ) -> None:
        if not self.current_contest or not self.current_task:
            return
        if request.output_path and os.path.exists(request.output_path) and not force:
            force = await self.push_screen_wait(
                ConfirmScreen(f"Overwrite {request.output_path}?")
            )
            if not force:
                return
        org, comp = contest_ref(self.current_contest)
        task_id = str(self.current_task.get("id"))
        self.set_status("Downloading task data…")
        while True:
            try:
                results = await self.session.call(
                    download_task_data,
                    org,
                    comp,
                    task_id,
                    categories=request.categories,
                    output_dir=request.output_dir,
                    output_path=request.output_path,
                    force=force,
                    show_progress=False,
                )
                warnings = [str(item.get("warning")) for item in results if item.get("warning")]
                if warnings:
                    self.set_status(
                        f"Downloaded {len(results)} item(s), with warning: {warnings[0]}",
                        "error",
                    )
                else:
                    self.set_status(
                        f"Downloaded {len(results)} item(s).",
                        "success",
                    )
                return
            except RuntimeError as exc:
                if "Refusing to overwrite" in str(exc) and not force:
                    force = await self.push_screen_wait(
                        ConfirmScreen(f"{exc}\nOverwrite existing files?")
                    )
                    if force:
                        continue
                    return
                self._network_error("Download failed", exc)
                return
            except Exception as exc:
                self._network_error("Download failed", exc)
                return

    def action_submit(self) -> None:
        if not self.current_task:
            self.set_status("Select a task before submitting.", "error")
        elif not self.session.state:
            self.require_login("Sign in before submitting.")
        else:
            self.submit_flow()

    @work(group="submit-flow", exclusive=True)
    async def submit_flow(self) -> None:
        request = await self.push_screen_wait(
            SubmitScreen(source_required=runtime().submission_proxy)
        )
        if not request or not self.current_contest or not self.current_task:
            return
        contest_generation = self.contest_generation
        task_generation = self.task_generation
        org, comp = contest_ref(self.current_contest)
        task_id = str(self.current_task.get("id"))
        self.set_status("Uploading submission…")
        try:
            result = await self.session.call(
                create_submission,
                org,
                comp,
                task_id,
                request.output_path,
                request.source_path,
                request.note,
            )
            submission_id = (
                result.get("submissionID")
                or result.get("submissionId")
                or result.get("id")
            )
            if not submission_id:
                raise RuntimeError("Submission response did not contain an ID")
            if not self._task_is_current(
                contest_generation, task_generation, org, comp, task_id
            ):
                self.set_status(
                    f"Submission {submission_id} queued for {org}/{comp} task "
                    f"{task_id}; selection changed, so polling was not started.",
                    "success",
                )
                return
            self.current_submission = {
                "id": str(submission_id),
                "state": "pending",
            }
            self.submission_generation += 1
            set_submission(self.current_submission)
            self.active_view = 3
            self.active_pane = "right"
            self._show_active_view()
            self.query_one("#submission-detail", Static).update(
                submission_details(self.current_submission)
            )
            self.set_status(f"Submission {submission_id} queued.", "success")
            self._resume_pending_submission_poll_current()
        except Exception as exc:
            if self._task_is_current(
                contest_generation, task_generation, org, comp, task_id
            ):
                self._network_error("Submission failed", exc)

    @work(group="submission-poll", exclusive=True)
    async def poll_submission(
        self,
        submission_id: str,
        org: str,
        comp: str,
        task_id: str,
        contest_generation: int,
        task_generation: int,
        submission_generation: int | None = None,
    ) -> None:
        while self._task_is_current(
            contest_generation, task_generation, org, comp, task_id
        ) and self.active_view == 3 and self.session.state and (
            submission_generation is None
            or submission_generation == self.submission_generation
        ) and str((self.current_submission or {}).get("id")) == submission_id:
            try:
                submission = await self.session.call(
                    _load_submission_with_auth,
                    submission_id,
                    org=org,
                    comp=comp,
                    task_id=task_id,
                )
            except Exception as exc:
                if self._task_is_current(
                    contest_generation, task_generation, org, comp, task_id
                ):
                    self._network_error("Could not poll submission", exc)
                return
            if not self._task_is_current(
                contest_generation, task_generation, org, comp, task_id
            ) or (
                submission_generation is not None
                and submission_generation != self.submission_generation
            ) or self.active_view != 3 or not self.session.state or (
                str((self.current_submission or {}).get("id")) != submission_id
            ):
                return
            self.current_submission = submission
            set_submission(submission)
            self.query_one("#submission-detail", Static).update(
                submission_details(submission)
            )
            state = str(submission.get("state") or "").casefold()
            if state not in PENDING_STATES:
                self.set_status(
                    f"Submission {submission_id} finished.",
                    "success",
                )
                self.refresh_submissions()
                return
            self.set_status(f"Submission {submission_id}: {state or 'queued'}")
            await asyncio.sleep(SUBMISSION_POLL_INTERVAL)

    def _resume_pending_submission_poll_current(self) -> None:
        if not self.current_contest or not self.current_task or not self.current_submission:
            return
        org, comp = contest_ref(self.current_contest)
        self._resume_pending_submission_poll(
            str(self.current_submission.get("id") or ""),
            org,
            comp,
            str(self.current_task.get("id")),
            self.contest_generation,
            self.task_generation,
            self.submission_generation,
        )

    def _resume_pending_submission_poll(
        self,
        submission_id: str,
        org: str,
        comp: str,
        task_id: str,
        contest_generation: int,
        task_generation: int,
        submission_generation: int,
    ) -> None:
        if (
            not submission_id or not self.current_submission
            or not self.session.state or self.active_view != 3
        ):
            return
        state = str(self.current_submission.get("state") or "").casefold()
        if state not in PENDING_STATES:
            self.workers.cancel_group(self, "submission-poll")
            return
        key = (org, comp, task_id, submission_id, submission_generation)
        worker = getattr(self, "_submission_poller", None)
        if getattr(self, "_submission_poll_key", None) == key and worker and not worker.is_finished:
            return
        self._submission_poll_key = key
        self._submission_poller = self.poll_submission(
            submission_id,
            org,
            comp,
            task_id,
            contest_generation,
            task_generation,
            submission_generation,
        )

    def action_play_menu(self) -> None:
        if not self.current_contest:
            self.set_status("Select a contest before using Play.", "error")
        else:
            self.play_menu_flow()

    @work(group="play-menu", exclusive=True)
    async def play_menu_flow(self) -> None:
        action = await self.push_screen_wait(
            PlayMenu(
                str(self.play_snapshot.get("workspace_state") or "missing"),
                str(self.play_snapshot.get("image_state") or "missing"),
            )
        )
        if not action:
            return
        if action in {"delete-container", "delete-image"}:
            org, comp = contest_ref(self.current_contest or {})
            message = (
                f"Remove containers and network for {org}/{comp}?\n"
                "Workspace data will be preserved."
                if action == "delete-container"
                else f"Delete cached competition images for {org}/{comp}?\n"
                "Workspace data will be preserved; images must be pulled again."
            )
            confirmed = await self.push_screen_wait(
                ConfirmScreen(message)
            )
            if not confirmed:
                return
        await self.perform_play_action(action)

    def _render_play_operation(self) -> None:
        reference = contest_ref(self.current_contest) if self.current_contest else None
        operation = self.play_operations.get(reference, {})
        cancellable = operation.get("status") in {"queued", "running"}
        self.query_one("#play-cancel", Button).disabled = not cancellable
        if not operation:
            self.query_one("#play-operation", Static).update("")
            return
        elapsed = max(0, time.time() - float(operation.get("created_at") or time.time()))
        error = operation.get("error") or {}
        text = (f"Operation {operation.get('id')} · {operation.get('action')} · {operation.get('status')}\n"
                f"Stage: {operation.get('stage') or '?'} · {int(elapsed)}s\n{operation.get('message') or ''}")
        if error:
            text += "\n" + str(error.get("message") or error) + "\n" + "\n".join(error.get("logs") or [])
        self.query_one("#play-operation", Static).update(text)
        self._refresh_context_bindings()

    @on(Button.Pressed, "#play-cancel")
    def cancel_play_clicked(self) -> None:
        self.action_cancel_play()

    def action_cancel_play(self) -> None:
        if self.active_view == 4 and self.current_contest:
            reference = contest_ref(self.current_contest)
            operation = self.play_operations.get(reference, {})
            if operation.get("status") in {"queued", "running"}:
                self.cancel_play_operation(reference, str(operation["id"]))

    @work(group="play-cancel", exclusive=True)
    async def cancel_play_operation(self, reference: tuple[str, str], operation_id: str) -> None:
        try:
            operation = await asyncio.to_thread(self._manager().cancel, operation_id)
            self.play_operations[reference] = operation
            if self.current_contest and contest_ref(self.current_contest) == reference:
                self._render_play_operation()
                self.set_status(f"Operation {operation_id}: {operation.get('status')}")
        except Exception as exc:
            if self.current_contest and contest_ref(self.current_contest) == reference:
                self.set_status(f"Could not cancel operation: {exc}", "error")

    def _pause_logs(self) -> None:
        self.log_generation += 1
        self.logs_following = False
        self.workers.cancel_group(self, "play-log-follow")
        self.query_one("#play-follow", Button).label = "Follow logs"

    @on(Button.Pressed, "#play-follow")
    def follow_logs_clicked(self) -> None:
        self.action_toggle_logs()

    def action_toggle_logs(self) -> None:
        if self.active_view != 4 or not self.current_contest:
            return
        if self.logs_following:
            self._pause_logs()
            self.set_status("Logs paused · g resumes")
        else:
            reference = contest_ref(self.current_contest)
            if self.log_reference != reference:
                self.log_lines.clear()
                self.log_reference = reference
            self.logs_following = True
            self.log_generation += 1
            self.query_one("#play-follow", Button).label = "Pause logs"
            self.query_one("#play-log-scroll", VerticalScroll).add_class("-open")
            self.follow_play_logs(reference, self.log_generation)

    @work(group="play-log-follow", exclusive=True)
    async def follow_play_logs(self, reference: tuple[str, str], generation: int) -> None:
        replay = list(self.log_lines)[-200:]
        replay_index = 0
        try:
            async for line in self._manager().async_follow_logs(*reference):
                if not self.current_contest or contest_ref(self.current_contest) != reference or self.active_view != 4:
                    return
                if replay_index < len(replay) and line == replay[replay_index]:
                    replay_index += 1
                    continue
                replay_index = len(replay)
                viewport = self.query_one("#play-log-scroll", VerticalScroll)
                bottom = viewport.scroll_y >= viewport.max_scroll_y - 1
                self.log_lines.append(line[:8192])
                self.query_one("#play-live-logs", Static).update("\n".join(self.log_lines))
                if bottom:
                    self.call_after_refresh(viewport.scroll_end, animate=False)
            if generation == self.log_generation:
                self.set_status("Log stream ended · g retries", "error")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if generation == self.log_generation:
                self.set_status(f"Log stream disconnected: {exc} · g retries", "error")
        finally:
            if generation == self.log_generation and self.is_mounted:
                self.logs_following = False
                self.query_one("#play-follow", Button).label = "Follow logs"

    async def perform_play_action(self, action: str) -> None:
        if not self.current_contest:
            return
        org, comp = contest_ref(self.current_contest)
        self.active_view = 4
        self.active_pane = "right"
        self._show_active_view()
        self.set_status(f"Play {action}…")
        reference = (org, comp)
        accepted_id = None
        def selected() -> bool:
            return bool(self.current_contest and contest_ref(self.current_contest) == reference)
        try:
            client = self._manager()
            if action == "logs":
                logs_value = await asyncio.to_thread(client.logs, org, comp, tail=200)
                logs = str(logs_value.get("logs") or "")
                content = Text("RECENT LOGS\n\n", style="bold")
                content.append(logs or "No recent logs.")
                content.append("\n\np actions · r refresh", style="dim")
                self.query_one("#play-content", Static).update(content)
                self.set_status("Play logs loaded.", "success")
                return
            elif action == "open":
                open_info = await asyncio.to_thread(client.open_info, org, comp)
                url = open_info.get("jupyter_url")
                if not await asyncio.to_thread(webbrowser.open, str(url)):
                    raise RuntimeError(f"Open {url} manually")
                self.set_status("Jupyter opened.", "success")
                return
            elif action == "dashboard":
                url = f"{client.base_url}/nitro/"
                if not await asyncio.to_thread(webbrowser.open, url):
                    raise RuntimeError(f"Open {url} manually")
                self.set_status("Play dashboard opened.", "success")
                return
            else:
                accepted = await asyncio.to_thread(
                    client.action,
                    org,
                    comp,
                    action,
                    pull="missing" if action in {"play", "recreate"} else None,
                    wait_timeout=120 if action in {"play", "recreate"} else None,
                )
                accepted_id = str(accepted["operation_id"])
                operation = accepted.get("operation") or {"id": accepted_id, "action": action, "status": "queued", "stage": "queued", "created_at": time.time()}
                self.play_operations[reference] = operation
                if selected():
                    self._render_play_operation()
                def progress(event: dict) -> None:
                    if self.play_operations.get(reference, {}).get("id") != accepted_id:
                        return
                    self.play_operations[reference].update(status="running", stage=event.get("stage"), message=event.get("message"))
                    if selected():
                        self._render_play_operation()
                result = await _wait_for_play_operation(
                    client, accepted_id,
                    timeout=None if action in {"pull", "play", "recreate"} else 600,
                    progress=progress,
                )
                self.play_operations[reference] = {**operation, **result, "status": result.get("status") or "complete"}
            if selected():
                self._render_play_operation()
                self.set_status(f"Play {action} completed.", "success")
                self.refresh_play_status()
        except Exception as exc:
            if accepted_id:
                # Read the terminal record: cancellation can race completion.
                try:
                    self.play_operations[reference] = await asyncio.to_thread(client.operation, accepted_id)
                except Exception:
                    self.play_operations[reference].update(status="failed", message=str(exc))
            if selected():
                self._render_play_operation()
                self.set_status(f"Play {action} failed: {exc}", "error")


def run_tui() -> int:
    try:
        _migrate_manager_if_needed()
    except Exception as exc:
        print(f"Play manager migration failed: {exc}")
        return 1
    result = NitroTUI().run(mouse=True)
    return int(result or 0)

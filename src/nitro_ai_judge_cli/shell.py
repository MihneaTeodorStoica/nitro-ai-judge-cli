"""Interactive NAIJ session and local command completion."""

from __future__ import annotations

import os
import readline
import shlex
from typing import Any, Callable

from .completion import candidates
from .state import (
    atomic_write,
    cached_items,
    load_context,
    prepare_history,
    save_context,
    selected_contest,
    selected_submission,
    selected_task,
    set_contest,
    set_submission,
    set_task,
)


Dispatch = Callable[[list[str]], int]


def shell_prompt(context: dict[str, Any] | None = None) -> str:
    context = load_context() if context is None else context
    contest = selected_contest(context)
    task = selected_task(context)
    parts = ["naij"]
    if contest:
        parts.append(f"{contest[0]}/{contest[1]}")
    if task is not None:
        parts.append(str(task))
    return f"[{' '.join(parts)}] > "


def shell_help() -> None:
    print(
        """Commands:
  help [COMMAND]       Show shell or command help
  ls                   List contests, tasks, or submissions for the context
  use [ORG/COMP [TASK] | TASK]
                       Show or change the persistent context
  show                 Show the selected contest, task, or submission
  ..                   Move one context level up
  NUMBER               Select a cached contest, task, or submission
  q | quit | exit      Exit the shell

All regular `naij` commands are also accepted without the leading `naij`."""
    )


def setup_readline() -> None:
    history = prepare_history()
    try:
        readline.read_history_file(history)
    except (FileNotFoundError, OSError):
        pass

    readline.set_completer_delims(readline.get_completer_delims().replace("/", ""))
    cache: list[str] = []

    def completer(text: str, state: int) -> str | None:
        nonlocal cache
        if state == 0:
            line = readline.get_line_buffer()
            start = readline.get_begidx()
            try:
                words = shlex.split(line[:start])
            except ValueError:
                words = line[:start].split()
            cache = candidates([*words, text], load_context())
        return cache[state] if state < len(cache) else None

    readline.set_completer(completer)
    readline.parse_and_bind("tab: menu-complete")
    readline.parse_and_bind('"\\e[Z": menu-complete-backward')


def save_shell_history() -> None:
    lines = []
    for index in range(1, readline.get_current_history_length() + 1):
        item = readline.get_history_item(index)
        if item:
            lines.append(item)
    data = (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
    atomic_write(prepare_history(), data)


def _back() -> None:
    context = load_context()
    if selected_submission(context):
        context.pop("submission", None)
    elif selected_task(context) is not None:
        context.pop("task", None)
        context.pop("submission", None)
    elif selected_contest(context):
        context.pop("contest", None)
        context.pop("task", None)
        context.pop("submission", None)
    save_context(context)


def _contest_candidates(context: dict[str, Any]) -> list[dict[str, Any]]:
    cache = context.get("cache", {})
    bucket = cache.get("contests", {}) if isinstance(cache, dict) else {}
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    if not isinstance(bucket, dict):
        return result
    for items in bucket.values():
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            org = item.get("organizationSlug") or item.get("organization")
            comp = item.get("competitionSlug") or item.get("slug")
            key = (str(org or ""), str(comp or ""))
            if all(key) and key not in seen:
                seen.add(key)
                result.append(item)
    return result


def _numeric_select(token: str) -> bool:
    if not token.isdigit():
        return False
    context = load_context()
    contest = selected_contest(context)
    task = selected_task(context)
    number = int(token)

    if contest is None:
        items = _contest_candidates(context)
        if 1 <= number <= len(items):
            set_contest(items[number - 1])
            return True
        return False

    if task is None:
        items = cached_items("tasks", f"{contest[0]}/{contest[1]}")
        exact = next((item for item in items if str(item.get("id")) == token), None)
        if exact is not None:
            set_task(exact)
            return True
        if 1 <= number <= len(items):
            set_task(items[number - 1])
            return True
        return False

    items = cached_items("submissions", f"{contest[0]}/{contest[1]}/{task}")
    exact = next(
        (
            item
            for item in items
            if str(item.get("id", "")).split("-")[-1] == token
        ),
        None,
    )
    if exact is not None:
        set_submission(exact)
        return True
    if 1 <= number <= len(items):
        set_submission(items[number - 1])
        return True
    return False


def _safe_dispatch(dispatch: Dispatch, words: list[str]) -> int:
    try:
        return dispatch(words)
    except KeyboardInterrupt:
        print()
        return 130
    except SystemExit as exc:
        return int(exc.code or 0)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Error: {exc}")
        return 1


def run_shell(dispatch: Dispatch) -> int:
    setup_readline()
    print("Nitro AI Judge Interactive Shell. Type `help` for commands.")
    try:
        while True:
            try:
                line = input(shell_prompt()).strip()
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                print()
                return 0

            if not line:
                continue
            try:
                words = shlex.split(line)
            except ValueError as exc:
                print(f"Error: {exc}")
                continue
            if not words:
                continue
            command = words[0].casefold()
            if command in {"q", "quit", "exit"}:
                return 0
            if command == "help" and len(words) == 1:
                shell_help()
                continue
            if command == "help":
                _safe_dispatch(dispatch, [words[1], "--help"])
                continue
            if command in {"..", "back", "unselect"}:
                _back()
                continue
            if len(words) == 1 and _numeric_select(words[0]):
                continue
            _safe_dispatch(dispatch, words)
    finally:
        save_shell_history()

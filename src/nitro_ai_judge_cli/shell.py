"""Interactive NAIJ session and local command completion."""

from __future__ import annotations

import random
import shlex
from typing import Any, Callable

try:
    import readline
except ImportError:  # Windows has no standard-library readline module.
    readline = None  # type: ignore[assignment]

from .completion import COMMANDS, candidates
from .contests import find_task
from .state import (
    atomic_write,
    cached_items,
    load_context,
    mutate_context,
    prepare_history,
    selected_contest,
    selected_submission,
    selected_task,
    selected_task_number,
    set_contest,
    set_submission,
    set_task,
)


Dispatch = Callable[[list[str]], int]
SHELL_HINTS = (
    "Press Tab to complete commands and selections.",
    "Use `use ORG/COMP` to select a contest.",
    "Enter a displayed number to navigate.",
    "Use `cd ..` to move up one level.",
    "Run `play` to start the selected contest environment.",
    "Run `tui` for the full-screen contest cockpit.",
)


def shell_prompt(context: dict[str, Any] | None = None) -> str:
    context = load_context() if context is None else context
    contest = selected_contest(context)
    task = selected_task(context)
    parts = ["naij"]
    if contest:
        parts.append(f"{contest[0]}/{contest[1]}")
    if task is not None:
        parts.append(selected_task_number(context) or str(task))
    return f"[{' '.join(parts)}] > "


def shell_help() -> None:
    print(
        """Commands:
  help | h | ? [COMMAND]
                       Show shell or command help
  ls | l               List contests, tasks, or submissions for the context
  use | cd [ORG/COMP [TASK] | TASK]
                       Show or change the persistent context
  cd .. | ..           Move one context level up
  show                 Show the selected contest, task, or submission
  pwd                  Print the complete current selection
  NUMBER               Select a cached contest, task, or submission
  q | quit | exit      Exit the shell

All regular `naij` commands are also accepted without the leading `naij`."""
    )


def setup_readline() -> None:
    if readline is None:
        return
    history = prepare_history()
    try:
        readline.read_history_file(history)
    except (FileNotFoundError, OSError):
        pass

    readline.set_completer_delims(" \t\n")
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
            cache = candidates([*words, text], interactive=True)
        return cache[state] if state < len(cache) else None

    readline.set_completer(completer)
    bindings: tuple[str, ...]
    if "libedit" in (readline.__doc__ or ""):
        bindings = (
            "bind ^I rl_complete",
            "bind ^L ed-clear-screen",
            "bind ^R em-inc-search-prev",
            "bind ^P ed-prev-history",
            "bind ^N ed-next-history",
            "bind -k up ed-prev-history",
            "bind -k down ed-next-history",
            "bind ^A ed-move-to-beg",
            "bind ^E ed-move-to-end",
            "bind ^W ed-delete-prev-word",
            "bind ^U ed-kill-line",
            "bind ^K ed-kill-line",
            'bind "\\e[1;5D" ed-prev-word',
            'bind "\\e[1;5C" em-next-word',
        )
    else:
        bindings = (
            "tab: menu-complete",
            '"\\e[Z": menu-complete-backward',
            "Control-l: clear-screen",
            "Control-r: reverse-search-history",
            "Control-p: previous-history",
            "Control-n: next-history",
            '"\\e[A": previous-history',
            '"\\e[B": next-history',
            '"\\eOA": previous-history',
            '"\\eOB": next-history',
            "Control-a: beginning-of-line",
            "Control-e: end-of-line",
            "Control-w: unix-word-rubout",
            "Control-u: unix-line-discard",
            "Control-k: kill-line",
            '"\\e[H": beginning-of-line',
            '"\\e[F": end-of-line',
            '"\\eOH": beginning-of-line',
            '"\\eOF": end-of-line',
            '"\\e[1;5D": backward-word',
            '"\\e[1;5C": forward-word',
        )
    for binding in bindings:
        readline.parse_and_bind(binding)


def save_shell_history() -> None:
    if readline is None:
        return
    lines = []
    for index in range(1, readline.get_current_history_length() + 1):
        item = readline.get_history_item(index)
        if item:
            lines.append(item)
    data = (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
    atomic_write(prepare_history(), data)


def _back() -> None:
    def back(context: dict[str, Any]) -> None:
        if selected_submission(context):
            context.pop("submission", None)
        elif selected_task(context) is not None:
            context.pop("task", None)
            context.pop("submission", None)
        elif selected_contest(context):
            context.pop("contest", None)
            context.pop("task", None)
            context.pop("submission", None)

    mutate_context(back)


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


def _entity_select(token: str) -> bool:
    context = load_context()
    contest = selected_contest(context)
    task = selected_task(context)
    lowered = token.casefold()

    if contest is None:
        for item in _contest_candidates(context):
            org = item.get("organizationSlug") or item.get("organization")
            comp = item.get("competitionSlug") or item.get("slug")
            if f"{org}/{comp}".casefold() == lowered:
                set_contest(item)
                return True
        return False

    if task is None:
        items = cached_items("tasks", f"{contest[0]}/{contest[1]}")
        match = find_task(items, token)
    else:
        items = cached_items("submissions", f"{contest[0]}/{contest[1]}/{task}")
        match = next(
            (item for item in items if str(item.get("id", "")).casefold() == lowered),
            None,
        )
        if match is None:
            match = next(
                (
                    item
                    for item in items
                    if str(item.get("id", "")).split("-")[-1].casefold() == lowered
                ),
                None,
            )
    if match is None:
        return False
    if task is None:
        set_task(match)
    else:
        set_submission(match)
    return True


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
    print(f"Hint: {random.choice(SHELL_HINTS)}")
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
            if command in {"help", "h", "?"} and len(words) == 1:
                shell_help()
                continue
            if command in {"help", "h", "?"}:
                target = {"cd": "use", "l": "ls"}.get(
                    words[1].casefold(), words[1]
                )
                if target.casefold() in {
                    "help", "h", "?", "pwd", "q", "quit", "exit", "..",
                    "back", "unselect",
                }:
                    shell_help()
                else:
                    _safe_dispatch(dispatch, [target, "--help"])
                continue
            if command in {"..", "back", "unselect"} or (
                command == "cd" and words[1:] == [".."]
            ):
                _back()
                continue
            if command == "cd":
                words = ["use", *words[1:]]
                command = "use"
            elif command == "l":
                words = ["ls", *words[1:]]
                command = "ls"
            elif command == "pwd":
                words = ["use"]
                command = "use"
            if len(words) == 1 and command not in COMMANDS:
                if _entity_select(words[0]) or _numeric_select(words[0]):
                    continue
            _safe_dispatch(dispatch, words)
    finally:
        save_shell_history()

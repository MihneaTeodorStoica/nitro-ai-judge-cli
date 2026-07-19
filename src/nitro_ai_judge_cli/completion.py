"""Context-aware completion candidates and shell-script generators."""

from __future__ import annotations

import os
from typing import Any, Iterable

from .config import DEFAULT_PAGE_SIZE, DEFAULT_SUBMISSION_PAGE_SIZE, TASK_FILE_CATEGORIES
from .state import load_context, selected_contest, selected_task


COMMANDS = (
    "login", "contests", "tasks", "task", "download-data", "play", "submit",
    "submissions", "submission", "set-final", "unset-final", "use", "ls",
    "show", "completion",
)
GLOBAL_OPTIONS = ("--api-url", "--submission-proxy", "--state-dir", "--help")
OPTIONS = {
    "login": ("--username", "--help"),
    "contests": ("--page", "--page-size", "--all-pages", "--all", "--help"),
    "tasks": ("--help",),
    "task": ("--help",),
    "download-data": ("-c", "--category", "-d", "--out-dir", "-o", "--output", "-f", "--force", "--help"),
    "play": ("--gpu", "--no-gpu", "--port", "--proxy-port", "--bind", "--pull", "--wait-timeout", "--volumes", "--force", "-f", "--follow", "--help"),
    "submit": ("-o", "--output", "-s", "--source", "-n", "--note", "-w", "--wait", "--help"),
    "submissions": ("-a", "--author", "-p", "--page", "-n", "--page-size", "-m", "--mode", "--help"),
    "submission": ("--org", "--comp", "--task-id", "--help"),
    "set-final": ("--help",),
    "unset-final": ("--help",),
    "use": ("--clear", "--help"),
    "completion": ("--help",),
}
PLAY_ACTIONS = ("up", "start", "stop", "restart", "down", "logs", "ps", "status")
VALUE_OPTIONS = {
    "contests": {"--page", "--page-size"},
    "download-data": {"-c", "--category", "-d", "--out-dir", "-o", "--output"},
    "play": {"--port", "--proxy-port", "--bind", "--pull", "--wait-timeout"},
    "submit": {"-o", "--output", "-s", "--source", "-n", "--note"},
    "submissions": {
        "-a", "--author", "-p", "--page", "-n", "--page-size", "-m", "--mode",
    },
    "submission": {"--org", "--comp", "--task-id"},
}


def _matches(candidates: Iterable[str], prefix: str) -> list[str]:
    lowered = prefix.lower()
    matches: dict[str, str] = {}
    for item in candidates:
        if item.lower().startswith(lowered):
            matches.setdefault(item.casefold(), item)
    return sorted(matches.values(), key=str.lower)


def _filesystem(prefix: str) -> list[str]:
    expanded = os.path.expanduser(prefix or ".")
    directory, base = os.path.split(expanded)
    directory = directory or "."
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    original_directory = os.path.dirname(prefix)
    candidates = []
    for name in names:
        if not name.lower().startswith(base.lower()):
            continue
        path = os.path.join(directory, name)
        rendered = os.path.join(original_directory, name) if original_directory else name
        if os.path.isdir(path):
            rendered += "/"
        candidates.append(rendered)
    return sorted(candidates, key=str.lower)


def _cached_contests(context: dict[str, Any]) -> list[str]:
    cache = context.get("cache", {})
    bucket = cache.get("contests", {}) if isinstance(cache, dict) else {}
    values: list[dict[str, Any]] = []
    selected = context.get("contest")
    if isinstance(selected, dict):
        values.append(selected)
    if isinstance(bucket, dict):
        for items in bucket.values():
            if isinstance(items, list):
                values.extend(item for item in items if isinstance(item, dict))
    result = []
    for item in values:
        org = item.get("organizationSlug") or item.get("organization") or item.get("org")
        comp = item.get("competitionSlug") or item.get("slug") or item.get("comp")
        if org and comp:
            result.append(f"{org}/{comp}")
    return result


def _cached_tasks_for(
    context: dict[str, Any], contest: tuple[str, str] | None
) -> list[str]:
    if not contest:
        return []
    cache = context.get("cache", {})
    bucket = cache.get("tasks", {}) if isinstance(cache, dict) else {}
    items = bucket.get(f"{contest[0]}/{contest[1]}", []) if isinstance(bucket, dict) else []
    result = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("id") is not None:
            result.append(str(item["id"]))
    return result


def _cached_submissions(context: dict[str, Any]) -> list[str]:
    selected = selected_contest(context)
    task = selected_task(context)
    if not selected or task is None:
        return []
    cache = context.get("cache", {})
    bucket = cache.get("submissions", {}) if isinstance(cache, dict) else {}
    items = bucket.get(f"{selected[0]}/{selected[1]}/{task}", []) if isinstance(bucket, dict) else []
    result = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("id"):
            submission_id = str(item["id"])
            result.append(submission_id)
            short_id = submission_id.split("-")[-1]
            if short_id != submission_id:
                result.append(short_id)
    return result


def _cache_contains(context: dict[str, Any], kind: str, key: str) -> bool:
    cache = context.get("cache", {})
    bucket = cache.get(kind, {}) if isinstance(cache, dict) else {}
    return isinstance(bucket, dict) and key in bucket


def _positionals(command: str, words: list[str]) -> list[str]:
    value_options = VALUE_OPTIONS.get(command, set())
    result: list[str] = []
    skip_value = False
    for word in words:
        if skip_value:
            skip_value = False
        elif word in value_options:
            skip_value = True
        elif not word.startswith("-"):
            result.append(word)
    return result


def _explicit_contest(command: str, words: list[str]) -> tuple[str, str] | None:
    positionals = _positionals(command, words)
    for value in positionals:
        if "/" in value:
            org, comp = value.split("/", 1)
            if org and comp:
                return org, comp
    if command != "use" and len(positionals) >= 2:
        return positionals[0], positionals[1]
    return None


def _cache_scope(
    committed: list[str], prefix: str, context: dict[str, Any], interactive: bool
) -> tuple[str, str, tuple[str, ...]] | None:
    if prefix.startswith("-"):
        return None
    if not committed:
        if not interactive:
            return None
        contest = selected_contest(context)
        if not contest:
            return "contests", "all", ()
        task = selected_task(context)
        if task is None:
            return "tasks", f"{contest[0]}/{contest[1]}", contest
        return (
            "submissions",
            f"{contest[0]}/{contest[1]}/{task}",
            (contest[0], contest[1], task),
        )

    command = committed[0]
    arguments = committed[1:]
    if arguments and (
        arguments[-1] in VALUE_OPTIONS.get(command, set())
        or arguments[-1].startswith("-")
    ):
        return None
    contest = _explicit_contest(command, arguments) or selected_contest(context)
    if command in {"tasks", "play"}:
        return "contests", "all", ()
    if command == "use" or command in {
        "task", "download-data", "submit", "submissions"
    }:
        if contest:
            return "tasks", f"{contest[0]}/{contest[1]}", contest
        return "contests", "all", ()
    if command in {"submission", "set-final", "unset-final"}:
        task = selected_task(context)
        if contest and task is not None:
            return (
                "submissions",
                f"{contest[0]}/{contest[1]}/{task}",
                (contest[0], contest[1], task),
            )
    return None


def _fill_cache(
    context: dict[str, Any], scope: tuple[str, str, tuple[str, ...]] | None
) -> dict[str, Any]:
    if scope is None:
        return context
    kind, key, target = scope
    if _cache_contains(context, kind, key):
        return context
    try:
        from .api import ensure_fresh_state, get_auth
        from .contests import load_competitions, load_tasks
        from .state import load_state, update_cache
        from .submissions import get_username, load_submissions

        auth_state = load_state()
        if not auth_state:
            return context
        auth_state = ensure_fresh_state(auth_state)
        auth = get_auth(auth_state) if auth_state else None
        if not auth:
            return context
        cookies = (auth[0] or "", auth[1] or "")
        bearer = auth[2]
        if kind == "contests":
            items = load_competitions(
                cookies,
                bearer,
                page=None,
                page_size=DEFAULT_PAGE_SIZE,
                featured=None,
                all_pages=True,
            )
        elif kind == "tasks":
            items = load_tasks(cookies, bearer, target[0], target[1])
        else:
            items = []
            for mode in ("partial", "complete"):
                mode_items, _ = load_submissions(
                    cookies,
                    bearer,
                    target[0],
                    target[1],
                    target[2],
                    author=get_username(auth_state),
                    page=None,
                    page_size=DEFAULT_SUBMISSION_PAGE_SIZE,
                    mode=mode,
                )
                items.extend(mode_items)
        update_cache(kind, key, items)
        return load_context()
    except Exception:
        return context


def candidates(
    words: list[str],
    context: dict[str, Any] | None = None,
    *,
    interactive: bool = False,
) -> list[str]:
    """Return candidates, lazily filling local context when it was not supplied."""
    supplied_context = context is not None
    context = load_context() if context is None else context
    words = list(words)
    prefix = words[-1] if words else ""
    committed = words[:-1] if words else []
    if committed and committed[-1] == "--state-dir":
        return _filesystem(prefix)
    while committed:
        if committed[0] == "--submission-proxy":
            committed.pop(0)
            continue
        if committed[0] in {"--api-url", "--state-dir"}:
            committed = committed[2:]
            continue
        break
    command = committed[0] if committed else ""
    previous = committed[-1] if committed else ""
    if previous in {"--category", "-c"}:
        return _matches(TASK_FILE_CATEGORIES, prefix)
    if previous in {"--mode", "-m"}:
        return _matches(("partial", "complete", "both"), prefix)
    if previous == "--pull":
        return _matches(("always", "missing", "never"), prefix)
    if previous in {"--output", "-o", "--source", "-s", "--out-dir", "-d"}:
        return _filesystem(prefix)
    if not supplied_context:
        context = _fill_cache(
            context, _cache_scope(committed, prefix, context, interactive)
        )
    options = OPTIONS.get(command, ("--help",))
    include_options = options if not prefix else ()
    if not committed:
        entities: tuple[str, ...] = ()
        if interactive:
            contest = selected_contest(context)
            task = selected_task(context)
            if not contest:
                entities = tuple(_cached_contests(context))
            elif task is None:
                entities = tuple(_cached_tasks_for(context, contest))
            else:
                entities = tuple(_cached_submissions(context))
        return _matches((*COMMANDS, *GLOBAL_OPTIONS, *entities), prefix)
    if command == "completion":
        return _matches(("zsh", "bash", "fish", *include_options), prefix)
    if command == "play" and len(committed) == 1:
        return _matches((*PLAY_ACTIONS, *_cached_contests(context), *OPTIONS["play"]), prefix)
    if prefix.startswith("-"):
        return _matches(options, prefix)
    if command in {"tasks", "play"}:
        return _matches((*_cached_contests(context), *include_options), prefix)
    if command == "use":
        contest = _explicit_contest(command, committed[1:]) or selected_contest(context)
        return _matches(
            (*_cached_contests(context), *_cached_tasks_for(context, contest), *include_options),
            prefix,
        )
    if command in {"task", "download-data", "submit", "submissions"}:
        contest = _explicit_contest(command, committed[1:]) or selected_contest(context)
        return _matches(
            (*_cached_contests(context), *_cached_tasks_for(context, contest), *include_options),
            prefix,
        )
    if command in {"submission", "set-final", "unset-final"}:
        return _matches((*_cached_submissions(context), *include_options), prefix)
    return _matches(include_options, prefix)


def script(shell: str) -> str:
    if shell == "zsh":
        return """#compdef naij nitro-cli
_naij() {
  local -a reply
  reply=(\"${(@f)$(naij __complete -- ${words[2,-1]})}\")
  _describe 'NAIJ' reply
}
compdef _naij naij nitro-cli
"""
    if shell == "bash":
        return """_naij_complete() {
  local IFS=$'\\n'
  COMPREPLY=($(naij __complete -- \"${COMP_WORDS[@]:1}\"))
}
complete -F _naij_complete naij nitro-cli
"""
    if shell == "fish":
        return """complete -c naij -f -a '(naij __complete -- (commandline -opc)[2..-1])'
complete -c nitro-cli -f -a '(naij __complete -- (commandline -opc)[2..-1])'
"""
    raise ValueError(f"unsupported shell: {shell}")

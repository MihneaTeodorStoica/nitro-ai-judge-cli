"""Pure, offline completion candidates and shell-script generators."""

from __future__ import annotations

import os
from typing import Any, Iterable

from .config import TASK_FILE_CATEGORIES
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


def _matches(candidates: Iterable[str], prefix: str) -> list[str]:
    lowered = prefix.lower()
    return sorted({item for item in candidates if item.lower().startswith(lowered)}, key=str.lower)


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


def _cached_tasks(context: dict[str, Any]) -> list[str]:
    selected = selected_contest(context)
    if not selected:
        return []
    cache = context.get("cache", {})
    bucket = cache.get("tasks", {}) if isinstance(cache, dict) else {}
    items = bucket.get(f"{selected[0]}/{selected[1]}", []) if isinstance(bucket, dict) else []
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


def candidates(words: list[str], context: dict[str, Any] | None = None) -> list[str]:
    """Return candidates using only supplied/local context and the filesystem."""
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
    if not committed:
        return _matches((*COMMANDS, *GLOBAL_OPTIONS), prefix)
    command = committed[0]
    previous = committed[-1] if committed else ""
    if previous in {"--category", "-c"}:
        return _matches(TASK_FILE_CATEGORIES, prefix)
    if previous in {"--mode", "-m"}:
        return _matches(("partial", "complete", "both"), prefix)
    if previous == "--pull":
        return _matches(("always", "missing", "never"), prefix)
    if previous in {"--output", "-o", "--source", "-s", "--out-dir", "-d"}:
        return _filesystem(prefix)
    options = OPTIONS.get(command, ("--help",))
    include_options = options if not prefix else ()
    if command == "completion":
        return _matches(("zsh", "bash", "fish", *include_options), prefix)
    if command == "play" and len(committed) == 1:
        return _matches((*PLAY_ACTIONS, *_cached_contests(context), *OPTIONS["play"]), prefix)
    if prefix.startswith("-"):
        return _matches(options, prefix)
    if command in {"tasks", "play", "use"}:
        return _matches((*_cached_contests(context), *include_options), prefix)
    if command in {"task", "download-data", "submit", "submissions"}:
        return _matches((*_cached_contests(context), *_cached_tasks(context), *include_options), prefix)
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

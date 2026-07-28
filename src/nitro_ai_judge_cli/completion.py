"""Context-aware completion candidates and shell-script generators."""

from __future__ import annotations

import os
from typing import Any, Iterable

from .config import DEFAULT_PAGE_SIZE, DEFAULT_SUBMISSION_PAGE_SIZE, TASK_FILE_CATEGORIES
from .state import load_context, selected_contest, selected_submission, selected_task


COMMANDS = (
    "login", "logout", "tui", "contests", "tasks", "task", "download-data", "play", "submit",
    "submissions", "submission", "set-final", "unset-final", "use", "ls",
    "show", "cache", "completion",
)
SHELL_COMMANDS = (
    "help", "cd", "pwd", "l", "h", "?", "q", "quit", "exit", "..",
    "back", "unselect",
)
GLOBAL_OPTIONS = ("--api-url", "--submission-proxy", "--state-dir", "-V", "--version", "--help")
PLAY_ACTIONS = (
    "play", "pull", "start", "stop", "restart", "recreate",
    "delete-container", "delete-image", "delete-workspace", "logs", "status", "ps", "cancel", "open", "manager",
)
MANAGER_ACTIONS = (
    "install", "update", "status", "open", "start", "stop", "restart",
    "uninstall", "purge", "sync-credentials",
)
OPTION_GROUPS = {
    "login": (("--username",), ("--password-stdin",), ("--help",)),
    "tui": (("--help",),),
    "contests": (
        ("--page",), ("--page-size",), ("--all-pages",), ("--all",),
        ("--help",),
    ),
    "tasks": (("--help",),),
    "task": (("--help",),),
    "download-data": (
        ("-c", "--category"), ("-d", "--out-dir"), ("-o", "--output"),
        ("-f", "--force"), ("--list",), ("--help",),
    ),
    "submit": (
        ("-o", "--output"), ("-s", "--source"), ("-n", "--note"),
        ("-w", "--wait"), ("--wait-timeout",), ("--help",),
    ),
    "submissions": (
        ("-a", "--author"), ("-p", "--page"), ("-n", "--page-size"),
        ("-m", "--mode"), ("--help",),
    ),
    "submission": (
        ("--org",), ("--comp",), ("--task-id",), ("-w", "--wait"),
        ("--wait-timeout",), ("--help",),
    ),
    "set-final": (("--help",),),
    "unset-final": (("--help",),),
    "use": (("--clear",), ("--help",)),
    "ls": (("--offline",), ("--help",)),
    "show": (("--offline",), ("--help",)),
    "cache": (("--help",),),
    "completion": (("--help",),),
}
PLAY_OPTION_GROUPS = {
    "play": (
        ("--gpu", "--no-gpu"), ("--pull",), ("--wait-timeout",),
        ("--open",), ("--yes",), ("--help",),
    ),
    "recreate": (
        ("--gpu", "--no-gpu"), ("--pull",), ("--wait-timeout",),
        ("--open",), ("--yes",), ("--help",),
    ),
    "pull": (("--pull",), ("--yes",), ("--help",)),
    "delete-workspace": (("--force",), ("--yes",), ("--help",)),
    "logs": (("-f", "--follow"), ("--tail",), ("--yes",), ("--help",)),
    "start": (("--yes",), ("--help",)),
    "stop": (("--yes",), ("--help",)),
    "restart": (("--yes",), ("--help",)),
    "delete-container": (("--yes",), ("--help",)),
    "delete-image": (("--yes",), ("--help",)),
    "status": (("--yes",), ("--help",)),
    "ps": (("--yes",), ("--help",)),
    "cancel": (("--yes",), ("--help",)),
    "open": (("--yes",), ("--help",)),
    "manager-install": (
        ("--bind",), ("--port",), ("--image",), ("--tls-cert",),
        ("--tls-key",), ("--public-url",), ("--yes",), ("--help",),
    ),
    "manager-update": (
        ("--bind",), ("--port",), ("--image",), ("--tls-cert",),
        ("--tls-key",), ("--public-url",), ("--yes",), ("--help",),
    ),
    "manager-purge": (("--force",), ("--help",)),
}
VALUE_OPTIONS = {
    "login": {"--username"},
    "contests": {"--page", "--page-size"},
    "download-data": {"-c", "--category", "-d", "--out-dir", "-o", "--output"},
    "play": {
        "--port", "--bind", "--pull", "--wait-timeout", "--tail", "--image",
        "--tls-cert", "--tls-key", "--public-url",
    },
    "submit": {
        "-o", "--output", "-s", "--source", "-n", "--note",
        "--wait-timeout",
    },
    "submissions": {
        "-a", "--author", "-p", "--page", "-n", "--page-size", "-m", "--mode",
    },
    "submission": {"--org", "--comp", "--task-id", "--wait-timeout"},
}
REPEATABLE_OPTION_GROUPS = {"download-data": {"-c"}}


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
    return [
        str(number)
        for number, item in enumerate(items if isinstance(items, list) else [], 1)
        if isinstance(item, dict) and item.get("id") is not None
    ]


def _cached_submissions_for(
    context: dict[str, Any], target: tuple[str, str, str] | None
) -> list[str]:
    if not target:
        return []
    cache = context.get("cache", {})
    bucket = cache.get("submissions", {}) if isinstance(cache, dict) else {}
    items = bucket.get("/".join(target), []) if isinstance(bucket, dict) else []
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


def _contest_ref(value: str, context: dict[str, Any]) -> tuple[str, str] | None:
    if "/" not in value:
        return None
    org, comp = value.split("/", 1)
    if not org or not comp:
        return None
    wanted = value.casefold()
    for candidate in _cached_contests(context):
        if candidate.casefold() == wanted:
            return tuple(candidate.split("/", 1))  # type: ignore[return-value]
    return org, comp


def _selected_submission_target(
    context: dict[str, Any],
) -> tuple[str, str, str] | None:
    contest = selected_contest(context)
    task = selected_task(context)
    if not contest or task is None:
        return None
    return contest[0], contest[1], task


def _option_groups(
    command: str, action: str | None = None
) -> tuple[tuple[str, ...], ...]:
    if command == "play":
        return PLAY_OPTION_GROUPS.get(action or "play", (("--help",),))
    return OPTION_GROUPS.get(command, (("--help",),))


def _remaining_options(
    command: str, words: list[str], prefix: str, action: str | None = None
) -> list[str]:
    groups = _option_groups(command, action)
    used: set[str] = set()
    repeatable = REPEATABLE_OPTION_GROUPS.get(command, set())
    for word in words:
        option = word.split("=", 1)[0]
        for group in groups:
            if option in group and group[0] not in repeatable:
                used.add(group[0])
                break
    remaining = [
        option
        for group in groups
        if group[0] not in used
        for option in group
    ]
    return _matches(remaining, prefix)


def _selected_option_values(words: list[str], aliases: set[str]) -> set[str]:
    selected: set[str] = set()
    for index, word in enumerate(words):
        if word in aliases and index + 1 < len(words):
            selected.add(words[index + 1].casefold())
        elif "=" in word and word.split("=", 1)[0] in aliases:
            selected.add(word.split("=", 1)[1].casefold())
    return selected


def _value_candidates(
    command: str, words: list[str], prefix: str, action: str | None = None
) -> list[str] | None:
    if not words:
        return None
    previous = words[-1]
    if previous not in VALUE_OPTIONS.get(command, set()):
        return None
    if command == "play" and not any(
        previous in group for group in _option_groups(command, action)
    ):
        return None
    if previous in {"--category", "-c"}:
        selected = _selected_option_values(words[:-1], {"--category", "-c"})
        return _matches(
            (item for item in TASK_FILE_CATEGORIES if item.casefold() not in selected),
            prefix,
        )
    if previous in {"--mode", "-m"}:
        return _matches(("partial", "complete", "both"), prefix)
    if previous == "--pull":
        return _matches(("always", "missing", "never"), prefix)
    if previous in {"--output", "-o", "--source", "-s", "--out-dir", "-d"}:
        return _filesystem(prefix)
    if previous in VALUE_OPTIONS.get(command, set()):
        return []
    return None


def _entity_scope(
    entity: tuple[str, tuple[str, ...] | None]
) -> tuple[str, str, tuple[str, ...]]:
    kind, target = entity
    if kind == "contests":
        return kind, "all", ()
    assert target is not None
    return kind, "/".join(target), target


def _entity_values(
    context: dict[str, Any], entities: list[tuple[str, tuple[str, ...] | None]]
) -> list[str]:
    values: list[str] = []
    for kind, target in entities:
        if kind == "contests":
            values.extend(_cached_contests(context))
        elif kind == "tasks":
            values.extend(_cached_tasks_for(context, target))  # type: ignore[arg-type]
        else:
            values.extend(_cached_submissions_for(context, target))  # type: ignore[arg-type]
    return values


def _root_entities(
    context: dict[str, Any]
) -> list[tuple[str, tuple[str, ...] | None]]:
    contest = selected_contest(context)
    if not contest:
        return [("contests", None)]
    task = selected_task(context)
    if task is None:
        return [("tasks", contest)]
    return [("submissions", (contest[0], contest[1], task))]


def _use_entities(
    words: list[str], prefix: str, context: dict[str, Any]
) -> list[tuple[str, tuple[str, ...] | None]] | None:
    positionals = _positionals("use", words)
    selected = selected_contest(context)
    if not positionals:
        if prefix:
            result: list[tuple[str, tuple[str, ...] | None]] = []
            if selected:
                result.append(("tasks", selected))
            result.append(("contests", None))
            return result
        return [("tasks", selected)] if selected else [("contests", None)]
    explicit = _contest_ref(positionals[0], context)
    if explicit and len(positionals) == 1:
        return [("tasks", explicit)]
    return None


def _task_entities(
    command: str, words: list[str], prefix: str, context: dict[str, Any]
) -> list[tuple[str, tuple[str, ...] | None]] | None:
    positionals = _positionals(command, words)
    selected = selected_contest(context)
    if positionals:
        explicit = _contest_ref(positionals[0], context)
        if explicit and len(positionals) == 1:
            return [("tasks", explicit)]
        if len(positionals) == 2 and all("/" not in item for item in positionals):
            return [("tasks", (positionals[0], positionals[1]))]
        return None
    if prefix:
        result: list[tuple[str, tuple[str, ...] | None]] = []
        if selected:
            result.append(("tasks", selected))
        result.append(("contests", None))
        return result
    if selected_task(context) is not None:
        return None
    return [("tasks", selected)] if selected else [("contests", None)]


def _tasks_entities(
    words: list[str], prefix: str, context: dict[str, Any]
) -> list[tuple[str, tuple[str, ...] | None]] | None:
    if _positionals("tasks", words):
        return None
    if prefix or not selected_contest(context):
        return [("contests", None)]
    return None


def _submission_entities(
    command: str, words: list[str], prefix: str, context: dict[str, Any]
) -> list[tuple[str, tuple[str, ...] | None]] | None:
    if _positionals(command, words):
        return None
    target = _selected_submission_target(context)
    if prefix or selected_submission(context) is None:
        return [("submissions", target)] if target else None
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


def _complete_entities(
    context: dict[str, Any],
    entities: list[tuple[str, tuple[str, ...] | None]],
    prefix: str,
    *,
    supplied_context: bool,
) -> list[str]:
    if not supplied_context:
        for entity in entities:
            context = _fill_cache(context, _entity_scope(entity))
    return _matches(_entity_values(context, entities), prefix)


def _play_action(words: list[str]) -> tuple[str, list[str]]:
    positionals = _positionals("play", words)
    if positionals:
        action = next(
            (item for item in PLAY_ACTIONS if item.casefold() == positionals[0].casefold()),
            None,
        )
        if action:
            return action, positionals[1:]
    return "play", positionals


def _play_completion_action(words: list[str]) -> str:
    action, remaining = _play_action(words)
    if action == "manager" and remaining:
        manager_action = next(
            (
                item
                for item in MANAGER_ACTIONS
                if item.casefold() == remaining[0].casefold()
            ),
            None,
        )
        if manager_action:
            return f"manager-{manager_action}"
    return action


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
    global_words: list[str] = []
    while committed:
        if committed[0] in {"--submission-proxy", "--help"}:
            global_words.append(committed.pop(0))
            continue
        if committed[0] in {"--api-url", "--state-dir"}:
            if len(committed) == 1:
                return _filesystem(prefix) if committed[0] == "--state-dir" else []
            global_words.extend(committed[:2])
            committed = committed[2:]
            continue
        break

    if not committed:
        if prefix.startswith("-"):
            remaining = [item for item in GLOBAL_OPTIONS if item not in global_words]
            return _matches(remaining, prefix)
        if not interactive:
            return _matches(COMMANDS, prefix)
        root_entities = _root_entities(context)
        local = _complete_entities(
            context, root_entities, prefix, supplied_context=supplied_context
        )
        if not prefix:
            return local
        return _matches((*COMMANDS, *SHELL_COMMANDS, *local), prefix)

    entered_command = committed[0]
    aliases = {"cd": "use", "l": "ls"}
    command = aliases.get(entered_command.casefold(), entered_command.casefold())
    arguments = committed[1:]
    if interactive and command in {"h", "?", "help"}:
        if _positionals(command, arguments):
            return []
        return _matches((*COMMANDS, *SHELL_COMMANDS), prefix)
    if interactive and command in {"pwd", "q", "quit", "exit", "..", "back", "unselect"}:
        return []
    if entered_command not in COMMANDS and not (
        interactive and entered_command.casefold() in aliases
    ):
        return []

    action = _play_completion_action(arguments) if command == "play" else None
    value_candidates = _value_candidates(command, arguments, prefix, action)
    if value_candidates is not None:
        return value_candidates
    if prefix.startswith("-"):
        return _remaining_options(command, arguments, prefix, action)

    if command == "completion":
        if _positionals(command, arguments):
            return _remaining_options(command, arguments, prefix)
        return _matches(("zsh", "bash", "fish", "powershell"), prefix)

    if command == "cache":
        positionals = _positionals(command, arguments)
        if not positionals:
            return _matches(("status", "clear"), prefix)
        if positionals[0] == "clear" and len(positionals) == 1:
            return _matches(("contests", "tasks", "submissions", "all"), prefix)
        return _remaining_options(command, arguments, prefix)

    if command == "play":
        positionals = _positionals(command, arguments)
        if not positionals:
            if not prefix:
                return list(PLAY_ACTIONS)
            contests = _complete_entities(
                context, [("contests", None)], prefix,
                supplied_context=supplied_context,
            )
            return _matches((*PLAY_ACTIONS, *contests), prefix)
        base_action, contest_words = _play_action(arguments)
        if base_action == "manager":
            if not contest_words:
                return _matches(MANAGER_ACTIONS, prefix)
            manager_action = next(
                (
                    item
                    for item in MANAGER_ACTIONS
                    if item.casefold() == contest_words[0].casefold()
                ),
                None,
            )
            if manager_action:
                return _remaining_options(
                    command,
                    arguments,
                    prefix,
                    f"manager-{manager_action}",
                )
            return _matches(MANAGER_ACTIONS, prefix)
        action = base_action
        if contest_words:
            return _remaining_options(command, arguments, prefix, action)
        if prefix:
            return _complete_entities(
                context, [("contests", None)], prefix,
                supplied_context=supplied_context,
            )
        if selected_contest(context):
            return _remaining_options(command, arguments, prefix, action)
        return _complete_entities(
            context, [("contests", None)], prefix,
            supplied_context=supplied_context,
        )

    entities: list[tuple[str, tuple[str, ...] | None]] | None = None
    if command == "use":
        entities = _use_entities(arguments, prefix, context)
    elif command in {"task", "download-data", "submit", "submissions"}:
        entities = _task_entities(command, arguments, prefix, context)
    elif command == "tasks":
        entities = _tasks_entities(arguments, prefix, context)
    elif command in {"submission", "set-final", "unset-final"}:
        entities = _submission_entities(command, arguments, prefix, context)
    if entities is not None:
        return _complete_entities(
            context, entities, prefix, supplied_context=supplied_context
        )
    return _remaining_options(command, arguments, prefix)


def script(shell: str) -> str:
    if shell == "zsh":
        return """#compdef naij nitro-cli
_naij() {
  local -a reply
  reply=(\"${(@f)$(naij __complete -- \"${words[@]:1}\")}\")
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
        return """function __naij_complete
  set -l tokens (commandline -opc)
  set -l current (commandline -ct)
  test (count $current) -eq 0; and set current ''
  naij __complete -- $tokens[2..-1] $current
end
complete -c naij -f -a '(__naij_complete)'
complete -c nitro-cli -f -a '(__naij_complete)'
"""
    if shell == "powershell":
        return r'''Register-ArgumentCompleter -Native -CommandName naij,nitro-cli -ScriptBlock {
  param($wordToComplete, $commandAst, $cursorPosition)
  $words = @(
    $commandAst.CommandElements | Select-Object -Skip 1 | ForEach-Object {
      if ($_ -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        $_.Value
      } else {
        $_.Extent.Text
      }
    }
  )
  if ($wordToComplete -and $words.Count) {
    $words = @($words | Select-Object -SkipLast 1)
  }
  $words += $wordToComplete
  naij __complete -- @words | ForEach-Object {
    $candidate = $_
    $completion = if ($candidate -match "[\s'\"]") {
      "'" + $candidate.Replace("'", "''") + "'"
    } else {
      $candidate
    }
    [System.Management.Automation.CompletionResult]::new(
      $completion, $candidate, 'ParameterValue', $candidate
    )
  }
}
'''
    raise ValueError(f"unsupported shell: {shell}")

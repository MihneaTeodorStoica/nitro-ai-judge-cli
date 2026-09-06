"""Argument parsing, context resolution, and command dispatch."""

from __future__ import annotations

import argparse
from contextlib import nullcontext, redirect_stdout
import sys
from typing import Any

from . import __version__
from .api import cmd_login, configure_runtime, get_auth, require_auth
from .completion import candidates as completion_candidates, script as completion_script
from .config import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_SUBMISSION_PAGE_SIZE,
)
from .contests import (
    cmd_contests,
    cmd_download_data,
    cmd_task,
    cmd_tasks,
    find_task,
    load_tasks,
    print_competitions,
    print_task,
    print_tasks,
    task_number,
)
from .play import (
    PLAY_ACTIONS,
    add_play_parser,
    cmd_play,
    normalize_play_argv,
    positive_seconds,
)
from .shell import run_shell
from .state import (
    CredentialsError,
    clear_credentials,
    clear_cache,
    clear_context,
    configure_state_dir,
    load_context,
    load_state,
    save_context,
    selected_contest,
    selected_submission,
    selected_task,
    selected_task_number,
    set_contest,
    set_task,
    update_cache,
)
from .submissions import (
    cmd_set_final,
    cmd_submission,
    cmd_submissions,
    cmd_submit,
    get_username,
    print_submission_details,
    print_submissions,
)


LEGACY_WARNING = (
    "warning: `nitro-cli` is deprecated; use `naij`. "
    "It will be removed in 4.0.0."
)


def parse_competition_ref(parts: list[str]) -> tuple[str, str]:
    if len(parts) == 1 and "/" in parts[0]:
        org, comp = parts[0].split("/", 1)
        if org and comp:
            return org, comp
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    raise ValueError("competition must be <org>/<comp> or <org> <comp>")


def _resolve_contest(
    explicit: list[str], context: dict[str, Any]
) -> tuple[str, str, bool]:
    if explicit:
        org, comp = parse_competition_ref(explicit)
        return org, comp, False
    stored = selected_contest(context)
    if stored:
        return stored[0], stored[1], True
    raise ValueError("no contest selected; run `naij use ORG/COMP`")


def _resolve_task_target(
    targets: list[str], context: dict[str, Any]
) -> tuple[str, str, str, bool]:
    inherited = False
    stored_contest = selected_contest(context)
    stored_task = selected_task(context)

    if not targets:
        if not stored_contest or stored_task is None:
            raise ValueError("no task selected; run `naij use ORG/COMP TASK`")
        return stored_contest[0], stored_contest[1], stored_task, True

    if len(targets) == 1:
        if "/" in targets[0]:
            org, comp = parse_competition_ref(targets)
            if stored_contest == (org, comp) and stored_task is not None:
                return org, comp, stored_task, True
            raise ValueError("task is missing; run `naij use ORG/COMP TASK`")
        if not stored_contest:
            raise ValueError("contest is missing; run `naij use ORG/COMP`")
        return stored_contest[0], stored_contest[1], targets[0], False

    if len(targets) == 2 and "/" in targets[0]:
        org, comp = parse_competition_ref([targets[0]])
        return org, comp, targets[1], False

    if len(targets) == 2:
        org, comp = parse_competition_ref(targets)
        if stored_contest == (org, comp) and stored_task is not None:
            return org, comp, stored_task, True
        raise ValueError("task is missing; run `naij use ORG/COMP TASK`")

    if len(targets) == 3:
        org, comp = parse_competition_ref(targets[:2])
        return org, comp, targets[2], False

    raise ValueError("expected [<org>/<comp> | <org> <comp>] [task]")


def _resolve_task_reference(
    cookies: tuple[str, str],
    bearer: str,
    org: str,
    comp: str,
    reference: str,
) -> tuple[str, str]:
    tasks = load_tasks(cookies, bearer, org, comp)
    task = find_task(tasks, reference)
    if task is None or task.get("id") is None:
        raise RuntimeError(f"task {reference!r} was not found in {org}/{comp}")
    update_cache("tasks", f"{org}/{comp}", tasks)
    task_id = str(task["id"])
    return task_id, task_number(tasks, task_id)


def _context_error(error: ValueError) -> int:
    print(f"Error: {error}")
    return 1


def _play_onboarding() -> int:
    try:
        logged_in = bool(get_auth(load_state() or {}))
    except CredentialsError:
        logged_in = False
    steps = ["naij use ORG/COMP", "naij play"]
    if not logged_in:
        steps.insert(0, "naij login")
    print("Complete Play setup:")
    for number, step in enumerate(steps, 1):
        print(f"{number}. {step}")
    return 1


def _show_context(context: dict[str, Any]) -> int:
    contest = selected_contest(context)
    task = selected_task(context)
    submission = selected_submission(context)
    print(f"Contest: {contest[0]}/{contest[1]}" if contest else "Contest: (none)")
    if task is None:
        print("Task: (none)")
    else:
        task_data = context.get("task")
        title = task_data.get("title") if isinstance(task_data, dict) else None
        print(f"Task: {selected_task_number(context)}{f' ({title})' if title else ''}")
    print(f"Submission: {submission}" if submission else "Submission: (none)")
    return 0


def _cmd_use(args: argparse.Namespace) -> int:
    if args.clear:
        if args.selection:
            print("Error: --clear does not accept a selection")
            return 1
        clear_context()
        print("Context cleared")
        return 0
    if not args.selection:
        return _show_context(load_context())

    current = load_context()
    selection = args.selection
    if "/" in selection[0]:
        if len(selection) > 2:
            print("Error: expected ORG/COMP [TASK]")
            return 1
        try:
            org, comp = parse_competition_ref([selection[0]])
        except ValueError as exc:
            return _context_error(exc)
        task_token = selection[1] if len(selection) == 2 else None
    else:
        if len(selection) != 1:
            print("Error: use the ORG/COMP form when selecting a contest")
            return 1
        stored = selected_contest(current)
        if not stored:
            print("Error: no contest selected; run `naij use ORG/COMP`")
            return 1
        org, comp = stored
        task_token = selection[0]

    auth_data = require_auth()
    if not auth_data:
        return 1
    _, cookies, bearer = auth_data
    try:
        tasks = load_tasks(cookies, bearer, org, comp)
    except RuntimeError as exc:
        print(f"Error: could not select {org}/{comp}: {exc}")
        return 1
    task = find_task(tasks, task_token) if task_token is not None else None
    if task_token is not None and task is None:
        print(f"Error: task {task_token!r} was not found in {org}/{comp}")
        return 1

    contest_data: dict[str, Any] = {
        "organizationSlug": org,
        "competitionSlug": comp,
    }
    cache = current.get("cache", {})
    contests_cache = cache.get("contests", {}) if isinstance(cache, dict) else {}
    if isinstance(contests_cache, dict):
        for items in contests_cache.values():
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                item_org = item.get("organizationSlug") or item.get("organization")
                item_comp = item.get("competitionSlug") or item.get("slug")
                if (str(item_org), str(item_comp)) == (org, comp):
                    contest_data = item
                    break

    set_contest(contest_data)
    update_cache("contests", "selected", [contest_data])
    update_cache("tasks", f"{org}/{comp}", tasks)
    if task is not None:
        set_task(task)
    return _show_context(load_context())


def _cache_entry(
    context: dict[str, Any], kind: str, key: str
) -> list[dict[str, Any]] | None:
    cache = context.get("cache")
    bucket = cache.get(kind) if isinstance(cache, dict) else None
    if not isinstance(bucket, dict) or key not in bucket:
        return None
    items = bucket[key]
    if not isinstance(items, list):
        return None
    return [item for item in items if isinstance(item, dict)]


def _offline_error(message: str, command: str) -> int:
    print(f"Error: {message}; run `{command}` without --offline to refresh.")
    return 1


def _dispatch_offline(args: argparse.Namespace) -> int:
    context = load_context()
    contest = selected_contest(context)
    task_id = selected_task(context)
    submission_id = selected_submission(context)

    if args.cmd == "ls":
        if not contest:
            items = _cache_entry(context, "contests", "featured")
            if items is None:
                items = _cache_entry(context, "contests", "all")
            if items is None:
                return _offline_error("no cached competitions are available", "naij ls")
            print("[cached; freshness unavailable]")
            if items:
                print_competitions(items)
            else:
                print("No competitions found")
            return 0
        if task_id is None:
            items = _cache_entry(context, "tasks", f"{contest[0]}/{contest[1]}")
            if items is None:
                return _offline_error(
                    f"no cached tasks are available for {contest[0]}/{contest[1]}",
                    "naij ls",
                )
            print("[cached; freshness unavailable]")
            print_tasks(items)
            return 0
        key = f"{contest[0]}/{contest[1]}/{task_id}"
        items = _cache_entry(context, "submissions", key)
        if items is None:
            return _offline_error(
                f"no cached submissions are available for {key}", "naij ls"
            )
        print("[cached; freshness unavailable]")
        if not items:
            print_submissions([], "partial")
        for item in items:
            mode = str(
                item.get("_mode")
                or ("complete" if "completeTaskScore" in item else "partial")
            )
            print_submissions([item], mode)
        return 0

    if not contest:
        return _offline_error("no contest is selected", "naij use ORG/COMP")

    if submission_id:
        if task_id is None:
            return _offline_error(
                "the selected submission has no task context", "naij use"
            )
        key = f"{contest[0]}/{contest[1]}/{task_id}"
        items = _cache_entry(context, "submissions", key)
        submission = next(
            (
                item
                for item in items or []
                if str(item.get("id")) == submission_id
            ),
            None,
        )
        if submission is None:
            return _offline_error(
                f"submission {submission_id} is missing from the cached {key} list",
                "naij show",
            )
        selected = context.get("submission")
        if isinstance(selected, dict) and str(selected.get("id")) == submission_id:
            submission = {**submission, **selected}
        print("[cached; freshness unavailable]")
        print_submission_details(submission)
        return 0

    if task_id is not None:
        items = _cache_entry(context, "tasks", f"{contest[0]}/{contest[1]}")
        task = next(
            (item for item in items or [] if str(item.get("id")) == task_id), None
        )
        if task is None:
            return _offline_error(
                f"task {task_id} is missing from the cached task list", "naij show"
            )
        selected = context.get("task")
        if isinstance(selected, dict) and str(selected.get("id")) == task_id:
            task = {**task, **selected}
        if "statement" not in task:
            return _offline_error(
                f"cached details for task {task_id} are incomplete", "naij show"
            )
        display_id = selected_task_number(context) or task_id
        print("[cached; freshness unavailable]")
        if display_id != task_id:
            print(f"Task number: {display_id}")
        print_task(task_id, task)
        return 0

    cache = context.get("cache")
    bucket = cache.get("contests") if isinstance(cache, dict) else None
    cached_contest = None
    if isinstance(bucket, dict):
        for values in bucket.values():
            if not isinstance(values, list):
                continue
            cached_contest = next(
                (
                    item
                    for item in values
                    if isinstance(item, dict)
                    and selected_contest({"contest": item}) == contest
                ),
                None,
            )
            if cached_contest is not None:
                break
    if cached_contest is None:
        return _offline_error(
            f"{contest[0]}/{contest[1]} is missing from the competition cache",
            "naij show",
        )
    print("[cached; freshness unavailable]")
    print_competitions([cached_contest])
    return 0


def _cmd_doctor(_: argparse.Namespace) -> int:
    from .diagnostics import collect_diagnostics

    for key, value in collect_diagnostics().items():
        print(f"{key}: {value}")
    return 0


def _cmd_cache(args: argparse.Namespace) -> int:
    if args.cache_action == "clear":
        clear_cache(args.kind)
        print(f"Cleared {args.kind} cache.")
        return 0

    cache = load_context().get("cache")
    print("Cache status (freshness unavailable):")
    found = False
    if isinstance(cache, dict):
        for kind, bucket in sorted(cache.items()):
            if not isinstance(bucket, dict):
                continue
            if not bucket:
                print(f"{kind}: 0 scopes, 0 records")
                found = True
            for scope, items in sorted(bucket.items()):
                count = (
                    len([item for item in items if isinstance(item, dict)])
                    if isinstance(items, list)
                    else 0
                )
                print(f"{kind}/{scope}: {count} records")
                found = True
    if not found:
        print("(empty)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="naij",
        description="Nitro AI Judge CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  naij contests
  naij use algolymp/algolymp-preojia-ix-x 1
  naij download-data -d data
  naij play
  naij submit -o submission.csv
""",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--api-url",
        help="Backend API base URL (CLI, NAIJ_API_BASE_URL, NITRO_API_BASE_URL, PROXY_URL, then default)",
    )
    parser.add_argument(
        "--submission-proxy",
        action="store_true",
        help="Submit through the Contestant Cloud submission proxy",
    )
    parser.add_argument(
        "--state-dir",
        help="State directory (default: NAIJ_STATE_DIR, NITRO_STATE_DIR, then ~/.naij)",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    login = sub.add_parser("login", help="Login to Nitro Judge")
    login.add_argument("--username")
    login.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read one password line from non-interactive stdin",
    )

    sub.add_parser("logout", help="Remove saved CLI credentials")

    sub.add_parser("tui", help="Open the full-screen contest cockpit")
    sub.add_parser("doctor", help="Show read-only diagnostics")

    contests = sub.add_parser("contests", help="List competitions")
    contests.add_argument("--page", type=int, default=1)
    contests.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    contests.add_argument("--all-pages", action="store_true")
    contests.add_argument("--all", action="store_true", help="Include non-featured competitions")

    tasks = sub.add_parser("tasks", help="List tasks in a competition")
    tasks.add_argument("competition", nargs="*", help="[<org>/<comp> | <org> <comp>]")

    task = sub.add_parser("task", help="Get task details")
    task.add_argument("targets", nargs="*", help="[competition] [task]")

    download = sub.add_parser("download-data", help="Download task data files")
    download.add_argument("targets", nargs="*", help="[competition] [task]")
    download.add_argument("-c", "--category", action="append", metavar="CATEGORY",
                          help="Built-in or server-advertised category key (see --list)")
    download.add_argument("-d", "--out-dir")
    download.add_argument("-o", "--output", help="Output path for one category; - writes raw bytes to stdout")
    download.add_argument("-f", "--force", action="store_true", help="Overwrite files")
    download.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="List available task data without downloading",
    )

    add_play_parser(sub)

    submit = sub.add_parser("submit", help="Create a submission")
    submit.add_argument("targets", nargs="*", help="[competition] [task]")
    submit.add_argument("-o", "--output", required=True)
    submit.add_argument("-s", "--source")
    submit.add_argument("-n", "--note", default="")
    submit.add_argument("-w", "--wait", action="store_true")
    submit.add_argument("--wait-timeout", type=positive_seconds, default=180)

    submissions = sub.add_parser("submissions", help="List submissions for a task")
    submissions.add_argument("targets", nargs="*", help="[competition] [task]")
    submissions.add_argument("-a", "--author")
    submissions.add_argument("-p", "--page", type=int)
    submissions.add_argument("-n", "--page-size", type=int, default=DEFAULT_SUBMISSION_PAGE_SIZE)
    submissions.add_argument("-m", "--mode", choices=("partial", "complete", "both"), default="both")

    submission = sub.add_parser("submission", help="Get submission feedback/details")
    submission.add_argument("submission_id", nargs="?")
    submission.add_argument("--org")
    submission.add_argument("--comp")
    submission.add_argument("--task-id")
    submission.add_argument("-w", "--wait", action="store_true")
    submission.add_argument("--wait-timeout", type=positive_seconds, default=180)

    for name, help_text in (("set-final", "Mark a submission as final"), ("unset-final", "Unmark a submission as final")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("submission_id", nargs="?")

    use = sub.add_parser("use", help="Show or change persistent context")
    use.add_argument("selection", nargs="*")
    use.add_argument("--clear", action="store_true")

    listing = sub.add_parser("ls", help="List items at the selected context level")
    listing.add_argument("--offline", action="store_true", help="Use cached data only")
    show = sub.add_parser("show", help="Show the selected item")
    show.add_argument("--offline", action="store_true", help="Use cached data only")

    cache = sub.add_parser("cache", help="Inspect or clear cached data")
    cache_actions = cache.add_subparsers(
        dest="cache_action", required=True, metavar="ACTION"
    )
    cache_actions.add_parser("status", help="Show cached scopes and record counts")
    cache_clear = cache_actions.add_parser("clear", help="Clear cached data")
    cache_clear.add_argument(
        "kind",
        nargs="?",
        choices=("contests", "tasks", "submissions", "all"),
        default="all",
    )

    completion = sub.add_parser("completion", help="Generate native shell completion")
    completion.add_argument("shell", choices=("zsh", "bash", "fish", "powershell"))

    internal = sub.add_parser("__complete", add_help=False)
    internal.add_argument("words", nargs=argparse.REMAINDER)
    return parser


def _dispatch_authenticated(args: argparse.Namespace) -> int:
    stream_stdout = args.cmd == "download-data" and args.output == "-"
    if stream_stdout and (not args.category or len(args.category) != 1):
        print("Error: --output - requires exactly one --category", file=sys.stderr)
        return 1
    with redirect_stdout(sys.stderr) if stream_stdout else nullcontext():
        auth_data = require_auth()
    if not auth_data:
        return 1
    state, cookies, bearer = auth_data
    context = load_context()

    if args.cmd == "contests":
        return cmd_contests(
            cookies,
            bearer,
            args.page,
            args.page_size,
            None if args.all else True,
            all_pages=args.all_pages,
        )

    if args.cmd == "tasks":
        try:
            org, comp, inherited = _resolve_contest(args.competition, context)
        except ValueError as exc:
            return _context_error(exc)
        result = cmd_tasks(cookies, bearer, org, comp)
        if result and inherited:
            print("Hint: refresh the selection with `naij use ORG/COMP`.")
        return result

    if args.cmd in {"task", "download-data", "submit", "submissions"}:
        try:
            org, comp, task_id, inherited = _resolve_task_target(args.targets, context)
        except ValueError as exc:
            if stream_stdout:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            return _context_error(exc)
        display_task_id = selected_task_number(context) or task_id
        if not inherited:
            try:
                task_id, display_task_id = _resolve_task_reference(
                    cookies, bearer, org, comp, task_id
                )
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr if stream_stdout else sys.stdout)
                return 1
        if args.cmd == "task":
            display = (
                {"display_id": display_task_id}
                if display_task_id != task_id
                else {}
            )
            result = cmd_task(cookies, bearer, org, comp, task_id, **display)
        elif args.cmd == "download-data":
            result = cmd_download_data(
                cookies, bearer, org, comp, task_id,
                categories=args.category,
                output_dir=args.out_dir or ".",
                output_path=args.output,
                force=args.force,
                list_only=args.list_only,
            )
        elif args.cmd == "submit":
            result = cmd_submit(
                cookies, bearer, org, comp, task_id,
                args.output,
                args.source,
                args.note,
                args.wait,
                args.wait_timeout,
            )
        else:
            result = cmd_submissions(
                cookies, bearer, org, comp, task_id,
                author=args.author or get_username(state),
                page=args.page,
                page_size=args.page_size,
                mode=args.mode,
            )
        if result and inherited:
            print(
                "Hint: refresh the selection with `naij use ORG/COMP TASK`.",
                file=sys.stderr if stream_stdout else sys.stdout,
            )
        return result

    if args.cmd == "submission":
        submission_id = args.submission_id or selected_submission(context)
        if not submission_id:
            return _context_error(ValueError("no submission selected; run `naij ls` and `naij use`"))
        contest = selected_contest(context)
        return cmd_submission(
            cookies,
            bearer,
            submission_id,
            org=args.org or (contest[0] if contest else None),
            comp=args.comp or (contest[1] if contest else None),
            task_id=args.task_id or selected_task(context),
            wait=args.wait,
            wait_timeout=args.wait_timeout,
        )

    if args.cmd in {"set-final", "unset-final"}:
        submission_id = args.submission_id or selected_submission(context)
        if not submission_id:
            return _context_error(ValueError("no submission selected; run `naij ls` and `naij use`"))
        contest = selected_contest(context)
        return cmd_set_final(
            cookies,
            bearer,
            submission_id,
            args.cmd == "set-final",
            org=contest[0] if contest else None,
            comp=contest[1] if contest else None,
            task_id=selected_task(context),
        )

    if args.cmd == "ls":
        contest = selected_contest(context)
        task_id = selected_task(context)
        if not contest:
            return cmd_contests(cookies, bearer, 1, DEFAULT_PAGE_SIZE, True)
        if task_id is None:
            result = cmd_tasks(cookies, bearer, contest[0], contest[1])
        else:
            result = cmd_submissions(
                cookies,
                bearer,
                contest[0],
                contest[1],
                task_id,
                author=get_username(state),
                page=None,
                page_size=DEFAULT_SUBMISSION_PAGE_SIZE,
                mode="both",
            )
        if result:
            print("Hint: refresh the selection with `naij use`.")
        return result

    if args.cmd == "show":
        contest = selected_contest(context)
        task_id = selected_task(context)
        submission_id = selected_submission(context)
        if submission_id:
            result = cmd_submission(
                cookies, bearer, submission_id,
                org=contest[0] if contest else None,
                comp=contest[1] if contest else None,
                task_id=task_id,
            )
            if result:
                print("Hint: refresh the selection with `naij use`.")
            return result
        if contest and task_id is not None:
            display_task_id = selected_task_number(context)
            display = (
                {"display_id": display_task_id}
                if display_task_id is not None and display_task_id != task_id
                else {}
            )
            result = cmd_task(
                cookies,
                bearer,
                contest[0],
                contest[1],
                task_id,
                **display,
            )
            if result:
                print("Hint: refresh the selection with `naij use`.")
            return result
        if contest:
            contest_value = context.get("contest")
            print_competitions([contest_value] if isinstance(contest_value, dict) else [])
            return 0
        return _context_error(ValueError("no context selected; run `naij use ORG/COMP`"))

    return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    index = 0
    while index < len(argv):
        option = argv[index].split("=", 1)[0]
        if option in {"--submission-proxy"}:
            index += 1
        elif option in {"--api-url", "--state-dir"}:
            index += 1 if "=" in argv[index] else 2
        else:
            break
    if index < len(argv) and argv[index] == "play":
        argv = [*argv[: index + 1], *normalize_play_argv(argv[index + 1 :])]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.state_dir is not None:
        configure_state_dir(args.state_dir)
    configure_runtime(args.api_url, args.submission_proxy)

    if args.cmd == "__complete":
        words = list(args.words)
        if words and words[0] == "--":
            words.pop(0)
        for item in completion_candidates(words):
            print(item)
        return 0
    if args.cmd == "completion":
        print(completion_script(args.shell), end="")
        return 0
    if args.cmd == "use":
        return _cmd_use(args)
    if args.cmd == "download-data" and args.list_only:
        conflicts = [
            flag
            for enabled, flag in (
                (args.category, "--category"),
                (args.out_dir is not None, "--out-dir"),
                (args.output is not None, "--output"),
                (args.force, "--force"),
            )
            if enabled
        ]
        if conflicts:
            print(f"Error: --list cannot be used with {', '.join(conflicts)}", file=sys.stderr if args.output == "-" else sys.stdout)
            return 1
    if args.cmd == "doctor":
        return _cmd_doctor(args)
    if args.cmd == "cache":
        return _cmd_cache(args)
    if args.cmd in {"ls", "show"} and args.offline:
        return _dispatch_offline(args)
    if not args.cmd:
        return run_shell(lambda words: main(words))
    if args.cmd == "login":
        password = None
        if args.password_stdin:
            if not args.username:
                print("Error: --username is required with --password-stdin.", file=sys.stderr)
                return 2
            if sys.stdin.isatty():
                print("Error: --password-stdin requires piped input.", file=sys.stderr)
                return 2
            password = sys.stdin.readline().rstrip("\r\n")
            if not password:
                print("Error: --password-stdin received an empty password.", file=sys.stderr)
                return 2
        result = cmd_login(args.username, password)
        if result == 0:
            from .play_manager_lifecycle import (
                load_manager_config,
                sync_manager_credentials,
            )

            if load_manager_config() is not None:
                try:
                    sync_manager_credentials(required=False)
                except Exception as exc:
                    print(
                        f"warning: Play manager login synchronization failed ({exc}); "
                        "run `naij play manager sync-credentials`",
                        file=sys.stderr,
                    )
        return result
    if args.cmd == "logout":
        try:
            removed = clear_credentials()
        except CredentialsError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print("Logged out." if removed else "Already logged out.")
        print(
            "Play manager credentials were not changed; run `naij play manager open` "
            "and choose Disconnect Nitro to remove them."
        )
        return 0
    if args.cmd == "tui":
        from .tui import run_tui

        return run_tui()
    if args.cmd == "play":
        if args.play_action in {"manager", "ls", "operations"}:
            return cmd_play(args)
        context = load_context()
        inherited = not getattr(args, "competition", None)
        if inherited:
            contest = selected_contest(context)
            if not contest:
                if args.play_action == "play":
                    return _play_onboarding()
                return _context_error(ValueError("no contest selected; run `naij use ORG/COMP`"))
            args.competition = [f"{contest[0]}/{contest[1]}"]
        result = cmd_play(args)
        if result and inherited:
            print("Hint: refresh the selection with `naij use ORG/COMP`.")
        return result
    return _dispatch_authenticated(args)


def legacy_main(argv: list[str] | None = None) -> int:
    print(LEGACY_WARNING, file=sys.stderr)
    return main(argv)

"""Versioned, read-only CLI output independent of human renderers."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import sys
from typing import Any

from . import contests, submissions, state
from .diagnostics import collect_diagnostics
from .play_manager_client import ManagerClient


def add_options(parser: argparse.ArgumentParser) -> None:
    def visit(node: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> None:
        allowed = (
            len(path) == 1 and path[0] in {
                "contests", "tasks", "task", "submissions", "submission", "use", "ls", "show", "doctor"
            }
            or path in {("play", "status"), ("play", "ps"), ("play", "ls"),
                        ("play", "operations"), ("play", "manager", "status")}
        )
        if allowed:
            node.add_argument("--json", action="store_true", help="Emit one schema-versioned JSON document")
        for action in node._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    visit(child, (*path, name))
    visit(parser)


def _numbered(tasks: list[dict]) -> list[dict]:
    return [{**task, "number": index} for index, task in enumerate(tasks, 1)]


def _mode(item: dict) -> dict:
    return {**item, "mode": item.get("mode") or item.get("_mode") or (
        "complete" if "completeTaskScore" in item and "partialTaskScore" not in item else "partial"
    )}


def _cached(context: dict, kind: str, key: str) -> list[dict]:
    bucket = (context.get("cache") or {}).get(kind) or {}
    if key not in bucket:
        raise ValueError(f"No cached {kind} for {key}; refresh without --offline first")
    return [item for item in bucket[key] if isinstance(item, dict)]


def collect(args: argparse.Namespace) -> dict[str, Any]:
    # Import lazily to avoid a CLI/parser import cycle.
    from . import cli
    command = args.cmd
    offline = bool(getattr(args, "offline", False))
    envelope = {"schema_version": 1, "kind": command, "cached": offline, "data": None}
    if command == "doctor":
        envelope["data"] = collect_diagnostics()
        return envelope
    if command == "play":
        client = ManagerClient.from_state()
        action = args.play_action
        envelope["kind"] = f"play.{action}"
        if action == "manager":
            envelope["kind"] = "play.manager.status"
            envelope["data"] = {"info": client.info(), "health": client.health()}
        elif action == "ls":
            envelope["data"] = client.competitions()
        elif action == "operations":
            envelope["data"] = client.operation_history(limit=args.limit, offset=args.offset, competition=args.reference, status=args.status, action=args.action)
        else:
            context = state.load_context()
            org, comp, _ = cli._resolve_contest(args.competition, context)
            envelope["data"] = client.competition(org, comp)
        return envelope
    context = state.load_context()
    if command == "use":
        if args.selection or args.clear:
            raise ValueError("use --json is read-only; omit the selection and --clear")
        envelope["data"] = {key: context.get(key) for key in ("contest", "task", "submission")}
        return envelope
    contest = state.selected_contest(context)
    task_id = state.selected_task(context)
    submission_id = state.selected_submission(context)
    if command in {"ls", "show"}:
        if command == "ls":
            command = "submissions" if task_id else "tasks" if contest else "contests"
        else:
            if not contest:
                raise ValueError("No contest selected; run naij use ORG/COMP")
            command = "submission" if submission_id else "task" if task_id else "contest"
        envelope["kind"] = command
    if offline:
        scope = "/".join(contest or ())
        if command == "contests":
            bucket = (context.get("cache") or {}).get("contests") or {}
            key = "featured" if "featured" in bucket else "all"
            data = _cached(context, "contests", key)
        elif command == "contest":
            data = context["contest"]
        elif command in {"tasks", "task"}:
            items = _numbered(_cached(context, "tasks", scope))
            data = items if command == "tasks" else next((item for item in items if str(item.get("id")) == task_id), None)
            if command == "task" and data:
                data = {**data, **(context.get("task") or {})}
        else:
            items = [_mode(item) for item in _cached(context, "submissions", f"{scope}/{task_id}")]
            data = items if command == "submissions" else next((item for item in items if str(item.get("id")) == submission_id), None)
            if command == "submission" and data:
                data = {**data, **(context.get("submission") or {})}
        if data is None:
            raise ValueError("Selected record is missing from cache; refresh without --offline")
        envelope["data"] = data
        return envelope
    auth = cli.require_auth()
    if not auth:
        raise ValueError("Authentication required; run naij login")
    credentials, cookies, bearer = auth
    if command == "contests":
        data = contests.load_competitions(
            cookies, bearer, page=getattr(args, "page", 1), page_size=getattr(args, "page_size", 20),
            featured=None if getattr(args, "all", False) else True, all_pages=getattr(args, "all_pages", False),
        )
    elif command == "contest":
        data = context["contest"]
    elif command == "tasks":
        org, comp, _ = cli._resolve_contest(getattr(args, "competition", []), context)
        data = _numbered(contests.load_tasks(cookies, bearer, org, comp))
    elif command in {"task", "submissions"}:
        org, comp, reference, inherited = cli._resolve_task_target(getattr(args, "targets", []), context)
        tasks = contests.load_tasks(cookies, bearer, org, comp)
        task = (next((item for item in tasks if str(item.get("id")) == reference), None)
                if inherited else contests.find_task(tasks, reference))
        if not task:
            raise ValueError(f"Task {reference} not found")
        task_id = str(task["id"])
        number = int(contests.task_number(tasks, task_id))
        if command == "task":
            data = {**task, **contests.load_task_view(cookies, bearer, org, comp, task_id).get("task", {}), "number": number}
        else:
            mode = getattr(args, "mode", "both")
            modes = ("partial", "complete") if mode == "both" else (mode,)
            data = []
            for mode in modes:
                items, pages = submissions.load_submissions(
                    cookies, bearer, org, comp, task_id,
                    author=getattr(args, "author", None) or submissions.get_username(credentials),
                    page=getattr(args, "page", None), page_size=getattr(args, "page_size", 20), mode=mode,
                )
                data.extend({**item, "mode": mode} for item in items)
    elif command == "submission":
        reference = getattr(args, "submission_id", None) or submission_id
        if not reference:
            raise ValueError("No submission selected")
        scope = dict(org=getattr(args, "org", None) or (contest[0] if contest else None),
                     comp=getattr(args, "comp", None) or (contest[1] if contest else None),
                     task_id=getattr(args, "task_id", None) or task_id)
        reference = submissions.resolve_submission_id(reference, cookies, bearer, **scope)
        if getattr(args, "wait", False):
            data = submissions.poll_submission_feedback(cookies, bearer, reference, timeout=args.wait_timeout, **scope)
        else:
            data = submissions.load_submission(reference, cookies, bearer, **scope)
        data = _mode(data)
    else:
        raise ValueError(f"JSON output is not supported for {command}")
    envelope["data"] = data
    return envelope


def dispatch(args: argparse.Namespace) -> int:
    try:
        with redirect_stdout(sys.stderr):
            value = collect(args)
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(encoded)
    if args.cmd == "doctor":
        code = int(value["data"].get("exit_status", 0))
        if code:
            print(value["data"].get("guidance", "Some diagnostic checks failed"), file=sys.stderr)
        return code
    return 0

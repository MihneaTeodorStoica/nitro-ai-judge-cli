"""Submission creation, listing, feedback, polling, and final selection."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from . import config
from .api import api_request_text, body_json, build_multipart, error_preview, parse_singlefetch, request_text
from .config import DEFAULT_SUBMISSION_PAGE_SIZE
from .state import load_state, update_cache
from .ui import format_datetime_ms

def get_username(state: dict[str, Any] | None) -> str:
    return (state or {}).get("username") or ""

def load_submission_metadata(
    cookies: tuple[str, str],
    bearer: str,
    org: str,
    comp: str,
    username: str,
    task_id: str,
) -> dict[str, Any] | None:
    if not username:
        return None
    status, body, _ = api_request_text(
        path=f"/organization/{org}/competition/{comp}/participant/{username}/submissionMetadata",
        bearer=bearer,
        params={"task_id": task_id},
    )
    if status != 200:
        return None
    data = body_json(body)
    return data if isinstance(data, dict) else None

def create_submission(
    cookies: tuple[str, str],
    bearer: str,
    org: str,
    comp: str,
    task_id: str,
    output_path: str,
    source_path: str | None,
    note: str,
) -> dict[str, Any]:
    note = note.strip() or "naij"
    if config.runtime().submission_proxy:
        if not source_path:
            raise RuntimeError("--source is required when using the submission proxy")
        payload = {
            "outputPath": output_path,
            "sourceCodePath": source_path,
            "note": note,
        }
        status, body, _ = api_request_text(
            path=f"/task/{task_id}/submit",
            bearer=bearer,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=300,
        )
        if status not in {200, 201}:
            raise RuntimeError(f"HTTP {status}: {error_preview(body)}")
        parsed = body_json(body)
        if not isinstance(parsed, dict):
            raise RuntimeError("Could not parse submission response")
        return parsed

    with open(output_path, "rb") as f:
        output_bytes = f.read()

    files = {
        "output": (os.path.basename(output_path), output_bytes, "text/csv"),
    }
    if source_path:
        with open(source_path, "rb") as f:
            source_bytes = f.read()
        files["sourceCode"] = (
            os.path.basename(source_path),
            source_bytes,
            "text/x-python",
        )

    data, boundary = build_multipart({"note": note}, files)
    status, body, _ = api_request_text(
        path=f"/organization/{org}/competition/{comp}/task/{task_id}/submit",
        bearer=bearer,
        method="POST",
        data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=300,
    )
    if status not in {200, 201}:
        raise RuntimeError(f"HTTP {status}: {error_preview(body)}")
    parsed = body_json(body)
    if not isinstance(parsed, dict):
        raise RuntimeError("Could not parse submission response")
    return parsed

def resolve_submission_id(
    submission_id: str,
    cookies: tuple[str, str],
    bearer: str,
    *,
    org: str | None = None,
    comp: str | None = None,
    task_id: str | None = None,
) -> str:
    if "-" in submission_id or not (org and comp and task_id):
        return submission_id

    try:
        author = get_username(load_state()) or None
    except RuntimeError:
        author = None

    candidates: list[str] = []
    for mode in ("partial", "complete"):
        items, _ = load_submissions(
            cookies,
            bearer,
            org,
            comp,
            task_id,
            author=author,
            page=None,
            page_size=DEFAULT_SUBMISSION_PAGE_SIZE,
            mode=mode,
        )
        for item in items:
            candidate = str(item.get("id") or "")
            if candidate.endswith(submission_id):
                candidates.append(candidate)

    candidates = sorted(set(candidates))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise RuntimeError(
            f"Multiple submissions match short id '{submission_id}': {', '.join(candidates)}"
        )
    raise RuntimeError(f"Could not resolve short submission id '{submission_id}'")

def load_submission(
    submission_id: str,
    cookies: tuple[str, str],
    bearer: str,
    *,
    org: str | None = None,
    comp: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    if org and comp and task_id:
        status, body, _ = request_text(
            path=f"/competitions/{org}/{comp}/{task_id}/submissions/{submission_id}.data",
            cookies=cookies,
        )
        if status == 200:
            parsed = parse_singlefetch(body)
            if parsed is None:
                raise RuntimeError("Could not parse submission details")
            root = parsed[0] if isinstance(parsed, list) and parsed else parsed
            submission = (
                (root.get("routes/task/submission/index") or {})
                .get("data", {})
                .get("submission")
            )
            if isinstance(submission, dict):
                return submission

    last_error = ""
    for mode in ("complete", "partial"):
        status, body, _ = api_request_text(
            path=f"/submission/{submission_id}",
            bearer=bearer,
            params={"scoring_mode": mode},
        )
        if status == 200:
            parsed = body_json(body)
            if isinstance(parsed, dict):
                return parsed
            raise RuntimeError("Could not parse submission details")
        last_error = f"HTTP {status}: {error_preview(body)}"
    raise RuntimeError(last_error or "Could not load submission")

def load_submissions(
    cookies: tuple[str, str],
    bearer: str,
    org: str,
    comp: str,
    task_id: str,
    *,
    author: str | None,
    page: int | None,
    page_size: int,
    mode: str,
) -> tuple[list[dict[str, Any]], int]:
    def fetch_api_page(target_page: int) -> tuple[list[dict[str, Any]], int] | None:
        status, body, _ = api_request_text(
            path=f"/organization/{org}/competition/{comp}/task/{task_id}/submissions",
            bearer=bearer,
            params={
                "author": author,
                "page": target_page,
                "page_size": page_size,
                "scoring_mode": mode,
            },
        )
        if status != 200:
            return None
        parsed = body_json(body)
        if isinstance(parsed, list):
            return parsed, 1
        if not isinstance(parsed, dict):
            return None
        items = (
            parsed.get("data")
            or parsed.get("items")
            or parsed.get("submissions")
            or parsed.get(
                "partialSubmissions" if mode == "partial" else "completeSubmissions"
            )
            or []
        )
        if isinstance(items, dict):
            items = items.get("data") or items.get("items") or []
        if not isinstance(items, list):
            return None
        last_page = int(parsed.get("lastPage") or parsed.get("last_page") or 1)
        return items, max(last_page, 1)

    if page is not None:
        api_page = fetch_api_page(page)
        if api_page is not None:
            return api_page
    else:
        api_page = fetch_api_page(1)
        if api_page is not None:
            items, last_page = api_page
            all_items = list(items)
            for next_page in range(2, last_page + 1):
                next_api_page = fetch_api_page(next_page)
                if next_api_page is None:
                    break
                page_items, _ = next_api_page
                all_items.extend(page_items)
            return all_items, last_page

    def fetch_page(target_page: int) -> tuple[list[dict[str, Any]], int]:
        status, body, _ = request_text(
            path=f"/competitions/{org}/{comp}/{task_id}/submissions.data",
            cookies=cookies,
            params={"author": author, "page": target_page, "page_size": page_size},
        )
        if status != 200:
            raise RuntimeError(f"HTTP {status}: {error_preview(body)}")
        parsed = parse_singlefetch(body)
        if parsed is None:
            raise RuntimeError("Could not parse submissions")
        root = parsed[0] if isinstance(parsed, list) and parsed else parsed
        data = (root.get("routes/task/submission/list") or {}).get("data", {})
        payload = (
            data.get(
                "partialSubmissions" if mode == "partial" else "completeSubmissions"
            )
            or {}
        )
        items = payload.get("data") or []
        last_page = int(data.get("lastPage") or 1)
        if not isinstance(items, list):
            raise RuntimeError("Could not parse submissions")
        return items, max(last_page, 1)

    if page is not None:
        return fetch_page(page)

    items, last_page = fetch_page(1)
    all_items = list(items)
    for next_page in range(2, last_page + 1):
        page_items, _ = fetch_page(next_page)
        all_items.extend(page_items)
    return all_items, last_page

def submission_score(submission: dict[str, Any], mode: str) -> str:
    key = "completeTaskScore" if mode == "complete" else "partialTaskScore"
    value = submission.get(key)
    return "In Queue" if value is None else f"{value} / 100"

def print_submissions(items: list[dict[str, Any]], mode: str) -> None:
    if not items:
        print("No submissions found")
        return
    for submission in items:
        short_id = str(submission.get("id", "")).split("-")[-1]
        timestamp = format_datetime_ms(submission.get("timestamp"))
        state = submission.get("state") or "?"
        final = " final" if submission.get("isFinal") else ""
        print(
            f"[{short_id}] {timestamp} | {submission_score(submission, mode)} | {state}{final}"
        )

def print_submission_details(submission: dict[str, Any]) -> None:
    print(f"Submission: {submission.get('id')}")
    print(f"User: {submission.get('username')}")
    print(f"Timestamp: {format_datetime_ms(submission.get('timestamp'))}")
    print(f"State: {submission.get('state')}")
    print(f"Final: {submission.get('isFinal')}")
    verdict = submission.get("verdictMessage") or "Success"
    print(f"Verdict: {verdict}")
    note = submission.get("note")
    if note:
        print(f"Note: {note}")
    print(f"Partial Score: {submission_score(submission, 'partial')}")
    if "completeTaskScore" in submission:
        print(f"Complete Score: {submission_score(submission, 'complete')}")
    subtasks = submission.get("subtasks") or []
    if subtasks:
        print("\nSubtasks:")
        for index, subtask in enumerate(subtasks):
            metric = subtask.get("metricName") or "metric"
            max_score = subtask.get("maximumScore") or "?"
            partial_score = (
                submission.get("partialSubtaskScores") or [None] * len(subtasks)
            )[index]
            partial_metric = (
                submission.get("partialSubtaskMetricValues") or [None] * len(subtasks)
            )[index]
            line = f"  #{subtask.get('id')} partial {partial_score}/{max_score}"
            if partial_metric is not None:
                line += f" | {metric}: {partial_metric}"
            if submission.get("completeTaskScore") is not None:
                complete_scores = submission.get("completeSubtaskScores") or [
                    None
                ] * len(subtasks)
                complete_metrics = submission.get("completeSubtaskMetricValues") or [
                    None
                ] * len(subtasks)
                line += f" | complete {complete_scores[index]}/{max_score}"
                if complete_metrics[index] is not None:
                    line += f" | {metric}: {complete_metrics[index]}"
            print(line)

def poll_submission_feedback(
    cookies: tuple[str, str],
    bearer: str,
    submission_id: str,
    *,
    org: str | None = None,
    comp: str | None = None,
    task_id: str | None = None,
    interval: int = 3,
    timeout: int = 180,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    while True:
        submission = load_submission(
            submission_id,
            cookies,
            bearer,
            org=org,
            comp=comp,
            task_id=task_id,
        )
        if submission.get("state") != "pending":
            return submission
        if time.time() >= deadline:
            raise RuntimeError("Timed out waiting for submission feedback")
        print("Waiting for feedback...", flush=True)
        time.sleep(interval)

def set_submission_final(
    cookies: tuple[str, str], bearer: str, submission_id: str, final: bool
) -> None:
    action = "setFinal" if final else "unsetFinal"
    status, body, _ = api_request_text(
        path=f"/submission/{submission_id}/{action}",
        bearer=bearer,
        method="POST",
    )
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {error_preview(body)}")

def cmd_submit(
    cookies: tuple[str, str],
    bearer: str,
    org: str,
    comp: str,
    task_id: str,
    output_path: str,
    source_path: str | None,
    note: str,
    wait: bool,
) -> int:
    try:
        submission = create_submission(
            cookies, bearer, org, comp, task_id, output_path, source_path, note
        )
    except (RuntimeError, OSError) as e:
        print(f"Error: {e}")
        return 1

    submission_id = submission.get("submissionID") or submission.get("submissionId")
    index = submission.get("submissionConsumptionIndex")
    print(f"Submission ID: {submission_id}")
    if index is not None:
        print(f"Submission Count: {index}")

    if wait and submission_id:
        try:
            feedback = poll_submission_feedback(
                cookies,
                bearer,
                submission_id,
                org=org,
                comp=comp,
                task_id=task_id,
            )
        except RuntimeError as e:
            print(f"Error: {e}")
            return 1
        print()
        print_submission_details(feedback)
    return 0

def cmd_submissions(
    cookies: tuple[str, str],
    bearer: str,
    org: str,
    comp: str,
    task_id: str,
    *,
    author: str | None,
    page: int | None,
    page_size: int,
    mode: str,
) -> int:
    modes = [mode] if mode in {"partial", "complete"} else ["partial", "complete"]
    cached: list[dict[str, Any]] = []
    for index, current_mode in enumerate(modes):
        try:
            items, last_page = load_submissions(
                cookies,
                bearer,
                org,
                comp,
                task_id,
                author=author,
                page=page,
                page_size=page_size,
                mode=current_mode,
            )
        except RuntimeError as e:
            print(f"Error: {e}")
            return 1
        if len(modes) > 1:
            if index:
                print()
            print(f"{current_mode.upper()} submissions (pages: {last_page})")
        print_submissions(items, current_mode)
        cached.extend(items)
    update_cache("submissions", f"{org}/{comp}/{task_id}", cached)
    return 0

def cmd_submission(
    cookies: tuple[str, str],
    bearer: str,
    submission_id: str,
    *,
    org: str | None = None,
    comp: str | None = None,
    task_id: str | None = None,
) -> int:
    try:
        submission_id = resolve_submission_id(
            submission_id,
            cookies,
            bearer,
            org=org,
            comp=comp,
            task_id=task_id,
        )
        submission = load_submission(
            submission_id,
            cookies,
            bearer,
            org=org,
            comp=comp,
            task_id=task_id,
        )
    except RuntimeError as e:
        print(f"Error: {e}")
        if not (org and comp and task_id):
            print(
                "Hint: try again with --org ORG --comp COMP --task-id TASK_ID if direct lookup is unavailable."
            )
        return 1
    print_submission_details(submission)
    return 0

def cmd_set_final(
    cookies: tuple[str, str],
    bearer: str,
    submission_id: str,
    final: bool,
    *,
    org: str | None = None,
    comp: str | None = None,
    task_id: str | None = None,
) -> int:
    try:
        submission_id = resolve_submission_id(
            submission_id,
            cookies,
            bearer,
            org=org,
            comp=comp,
            task_id=task_id,
        )
        set_submission_final(cookies, bearer, submission_id, final)
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1
    print(f"Submission {submission_id} {'set as' if final else 'unset as'} final")
    return 0

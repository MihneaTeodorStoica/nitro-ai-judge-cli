"""Competition, task, task-file, and download operations."""

from __future__ import annotations

from html.parser import HTMLParser
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import zipfile
from typing import Any

from .api import api_request_bytes, api_request_text, body_json, error_preview, int_payload, list_payload, parse_singlefetch, request, request_text
from .config import BASE_URL, DEFAULT_PAGE_SIZE, TASK_FILE_CATEGORIES, DEFAULT_TASK_FILE_CATEGORIES, TASK_FILE_PAGE_LABELS, MAX_PAGINATION_PAGES
from .state import update_cache
from .ui import _start_spinner, _stop_spinner, format_datetime_ms

ZIP_BOMB_MAX_FILES = 10_000
ZIP_BOMB_MAX_UNCOMPRESSED_BYTES = 5 * 1024**3
ZIP_BOMB_MAX_COMPRESSION_RATIO = 200
ZIP_BOMB_RATIO_MIN_BYTES = 100 * 1024**2

class TaskFileLinkParser(HTMLParser):
    def __init__(self, org: str, comp: str, task_id: str) -> None:
        super().__init__()
        self.org = org
        self.comp = comp
        self.task_id = str(task_id)
        self.links: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        category = task_file_category_from_href(self.org, self.comp, self.task_id, href)
        if category and category not in self.links:
            self.links[category] = href

def contest_phase(competition: dict[str, Any], now_ms: float | None = None) -> str:
    if now_ms is None:
        now_ms = time.time() * 1000

    start = competition.get("competitionStart")
    end = competition.get("competitionEnd")
    if isinstance(start, (int, float)) and now_ms < start:
        return "Upcoming"
    if isinstance(end, (int, float)) and now_ms >= end:
        return "Ended"
    return "Ongoing"

def grouped_competitions(
    competitions: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    now_ms = time.time() * 1000
    groups: list[tuple[str, list[dict[str, Any]]]] = [
        ("Ongoing", []),
        ("Upcoming", []),
        ("Ended", []),
    ]
    grouped = {label: items for label, items in groups}
    for competition in competitions:
        grouped[contest_phase(competition, now_ms)].append(competition)
    return [(label, items) for label, items in groups if items]

def normalize_task_file_category(category: str) -> str:
    normalized = category.strip().lower().replace("-", "_")
    aliases = {
        "pre_judging": "pre_judging_script",
        "prejudging": "pre_judging_script",
        "prejudging_script": "pre_judging_script",
        "pre_judge": "pre_judging_script",
        "pre_judge_script": "pre_judging_script",
    }
    normalized = aliases.get(normalized, normalized)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", normalized):
        raise ValueError(f"invalid file category '{category}'")
    return normalized

def filename_from_content_disposition(value: str) -> str | None:
    if not value:
        return None
    params: dict[str, str] = {}
    for part in value.split(";")[1:]:
        if "=" not in part:
            continue
        key, raw_value = part.split("=", 1)
        params[key.strip().lower()] = raw_value.strip().strip('"')
    filename = params.get("filename*") or params.get("filename")
    if not filename:
        return None
    if "''" in filename:
        _, filename = filename.split("''", 1)
        filename = urllib.parse.unquote(filename)
    filename = os.path.basename(filename.strip().strip('"').replace("\\", "/"))
    if any(char in filename for char in ("\x00", ":")) or filename in {".", ".."}:
        return None
    return filename or None

def task_file_category_from_href(
    org: str, comp: str, task_id: str, href: str
) -> str | None:
    path = urllib.parse.urlparse(href).path
    parts = [part for part in path.split("/") if part]
    expected = ["competitions", org, comp, str(task_id)]
    if len(parts) != 6 or parts[:4] != expected or parts[5] != "download":
        return None
    try:
        return normalize_task_file_category(parts[4])
    except ValueError:
        return None

def request_path_from_href(href: str) -> str:
    parsed = urllib.parse.urlparse(href)
    path = parsed.path
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path

def generic_task_filename(filename: str) -> bool:
    stem, _ = os.path.splitext(os.path.basename(filename))
    return stem.lower() in {"file", "download", "pre_judging_script"}

def task_file_content_type(headers: dict[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() == "content-type":
            return value.lower()
    return ""

def task_file_extension(
    category: str, headers: dict[str, str], body: bytes | None = None
) -> str:
    filename = ""
    for key, value in headers.items():
        if key.lower() == "content-disposition":
            filename = filename_from_content_disposition(value) or ""
            break
    _, extension = os.path.splitext(filename)

    if body:
        stripped = body.lstrip()
        if stripped.startswith(b"PK\x03\x04"):
            return ".zip"
        if stripped.startswith(b"{"):
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            if (
                isinstance(parsed, dict)
                and isinstance(parsed.get("cells"), list)
                and isinstance(parsed.get("metadata"), dict)
            ):
                return ".ipynb"

    if category == "pre_judging_script":
        return ".py"

    content_type = task_file_content_type(headers)
    if "text/csv" in content_type:
        return ".csv"
    if extension:
        return extension
    return ""

def task_file_name(
    category: str, headers: dict[str, str], body: bytes | None = None
) -> str:
    if category == "statement":
        return "TASK.md"
    for key, value in headers.items():
        if key.lower() == "content-disposition":
            filename = filename_from_content_disposition(value)
            if filename and not generic_task_filename(filename):
                return filename
    return f"{category}{task_file_extension(category, headers, body)}"

def response_is_html(body: bytes, headers: dict[str, str]) -> bool:
    content_type = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = value.lower()
            break
    return "text/html" in content_type or body.lstrip().lower().startswith(
        b"<!doctype html"
    )

def task_statement_markdown(
    cookies: tuple[str, str], bearer: str, org: str, comp: str, task_id: str
) -> bytes:
    payload = load_task_view(cookies, bearer, org, comp, task_id)
    task = payload["task"]
    title = task.get("title") or f"Task {task_id}"
    statement = task.get("statement") or ""
    content = f"# {title}\n\n{statement.strip()}\n"
    return content.encode("utf-8")

def task_has_statement(
    cookies: tuple[str, str], bearer: str, org: str, comp: str, task_id: str
) -> bool:
    try:
        payload = load_task_view(cookies, bearer, org, comp, task_id)
    except RuntimeError:
        return False
    task = payload["task"]
    return bool(task.get("statement"))

def load_competitions_page(
    cookies: tuple[str, str],
    bearer: str,
    *,
    page: int,
    page_size: int,
    featured: bool | None,
) -> tuple[list[dict[str, Any]], int]:
    featured_value = None if featured is None else ("true" if featured else "false")
    status, body, _ = api_request_text(
        path="/competitions",
        bearer=bearer,
        params={"page": page, "page_size": page_size, "featured": featured_value},
    )
    if status == 200:
        data = body_json(body)
        competitions = list_payload(data, "competitions", "items", "data")
        if competitions is not None:
            last_page = int_payload(
                data,
                "lastPage",
                "last_page",
                "totalPages",
                "total_pages",
                default=0,
            )
            if not last_page and len(competitions) < page_size:
                last_page = page
            return competitions, last_page

    status, body, _ = request_text(
        path="/competitions.data",
        cookies=cookies,
        params={"page": page, "page_size": page_size, "featured": featured_value},
    )
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {error_preview(body)}")

    data = parse_singlefetch(body)
    if data is None:
        raise RuntimeError("Could not parse response")

    root = data[0] if isinstance(data, list) and data else data
    competitions_data = (root.get("routes/competitions/index") or {}).get("data", {})
    competitions = (
        competitions_data.get("competitions") or competitions_data.get("items") or []
    )
    last_page = competitions_data.get("lastPage") or 1
    if not isinstance(competitions, list):
        raise RuntimeError("Unexpected competition data")
    return competitions, int(last_page)

def load_competitions(
    cookies: tuple[str, str],
    bearer: str,
    *,
    page: int | None,
    page_size: int,
    featured: bool | None,
    all_pages: bool = False,
) -> list[dict[str, Any]]:
    if page is not None and not all_pages:
        competitions, _ = load_competitions_page(
            cookies, bearer, page=page, page_size=page_size, featured=featured
        )
        return competitions

    competitions, last_page = load_competitions_page(
        cookies, bearer, page=1, page_size=page_size, featured=featured
    )
    all_competitions = list(competitions)
    if not competitions or last_page == 1:
        return all_competitions

    page_limit = min(
        max(last_page, 1) if last_page else MAX_PAGINATION_PAGES,
        MAX_PAGINATION_PAGES,
    )
    seen_pages = {repr(competitions)}
    for next_page in range(2, page_limit + 1):
        page_items, discovered_last_page = load_competitions_page(
            cookies,
            bearer,
            page=next_page,
            page_size=page_size,
            featured=featured,
        )
        signature = repr(page_items)
        if not page_items or signature in seen_pages:
            break
        all_competitions.extend(page_items)
        seen_pages.add(signature)
        if discovered_last_page and next_page >= min(
            discovered_last_page, MAX_PAGINATION_PAGES
        ):
            break
    return all_competitions

def print_competitions(competitions: list[dict[str, Any]]) -> None:
    for phase, phase_competitions in grouped_competitions(competitions):
        print(f"{phase}:")
        for competition in phase_competitions:
            org = competition.get("organizationSlug") or ""
            slug = competition.get("competitionSlug") or ""
            title = competition.get("title") or "?"
            print(f"[{org}/{slug}] {title}")
            start = competition.get("competitionStart")
            end = competition.get("competitionEnd")
            if start and end:
                print(f"  {format_datetime_ms(start)} -> {format_datetime_ms(end)}")
        print()

def cmd_contests(
    cookies: tuple[str, str],
    bearer: str,
    page: int | None,
    page_size: int,
    featured: bool | None,
    all_pages: bool = False,
) -> int:
    try:
        competitions = load_competitions(
            cookies,
            bearer,
            page=page,
            page_size=page_size,
            featured=featured,
            all_pages=all_pages,
        )
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1
    update_cache("contests", "all" if featured is None else "featured", competitions)
    print_competitions(competitions)
    return 0

def load_tasks(
    cookies: tuple[str, str], bearer: str, org: str, comp: str
) -> list[dict[str, Any]]:
    status, body, _ = api_request_text(
        path=f"/organization/{org}/competition/{comp}/tasks",
        bearer=bearer,
    )
    if status == 200:
        data = body_json(body)
        tasks = list_payload(data, "tasks", "items", "data")
        if tasks is not None:
            return tasks

    status, body, _ = request_text(
        path=f"/competitions/{org}/{comp}.data",
        cookies=cookies,
    )
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {error_preview(body)}")
    data = parse_singlefetch(body)
    if data is None:
        raise RuntimeError("Could not parse response")
    root = data[0] if isinstance(data, list) and data else data
    competition_layout = (root.get("routes/competition/layout") or {}).get("data", {})
    task_list = competition_layout.get("taskList") or []
    if not isinstance(task_list, list):
        raise RuntimeError("Could not parse response")
    return task_list

def find_task(tasks: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    if token.isdigit() and 1 <= int(token) <= len(tasks):
        return tasks[int(token) - 1]
    lowered = token.casefold()
    for task in tasks:
        if str(task.get("id", "")).casefold() == lowered:
            return task
    for task in tasks:
        if str(task.get("title", "")).casefold() == lowered:
            return task
    return None

def task_number(tasks: list[dict[str, Any]], task_id: str) -> str:
    for number, task in enumerate(tasks, 1):
        if str(task.get("id")) == str(task_id):
            return str(number)
    return str(task_id)

def print_tasks(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        print("No tasks found")
        return
    for task_id, task in enumerate(tasks, 1):
        title = task.get("title") or "?"
        print(f"[{task_id}] {title}")
        synopsis = task.get("synopsis")
        if synopsis:
            print(f"  {synopsis}")

def cmd_tasks(cookies: tuple[str, str], bearer: str, org: str, comp: str) -> int:
    try:
        tasks = load_tasks(cookies, bearer, org, comp)
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1
    update_cache("tasks", f"{org}/{comp}", tasks)
    print_tasks(tasks)
    return 0

def load_task_view(
    cookies: tuple[str, str], bearer: str, org: str, comp: str, task_id: str
) -> dict[str, Any]:
    status, body, _ = api_request_text(
        path=f"/organization/{org}/competition/{comp}/task/{task_id}",
        bearer=bearer,
    )
    if status == 200:
        parsed = body_json(body)
        if isinstance(parsed, dict):
            task = (
                parsed.get("task") if isinstance(parsed.get("task"), dict) else parsed
            )
            if task:
                return {"task": task, "loader": parsed, "root": parsed}

    status, body, _ = request_text(
        path=f"/competitions/{org}/{comp}/{task_id}/view.data",
        cookies=cookies,
    )
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {error_preview(body)}")

    data = parse_singlefetch(body)
    if data is None:
        raise RuntimeError("Could not parse response")

    root = data[0] if isinstance(data, list) and data else data
    task_layout = root.get("routes/task/layout", {})
    loader_data = task_layout.get("data", {})
    task = loader_data.get("task") or {}
    if not isinstance(task, dict) or not task:
        raise RuntimeError("Task not found")
    return {"task": task, "loader": loader_data, "root": root}

def print_task(task_id: str, task: dict[str, Any]) -> None:
    print(f"# {task.get('title') or task_id}")
    print(f"ID: {task_id}")
    print()
    print(task.get("statement") or "N/A")
    subtasks = task.get("subtasks") or []
    if subtasks:
        print(f"\nSubtasks: {len(subtasks)}")
        for index, subtask in enumerate(subtasks, 1):
            if isinstance(subtask, dict):
                title = subtask.get("title") or subtask.get("metricName") or "?"
                max_score = (
                    subtask.get("maxScore") or subtask.get("maximumScore") or "?"
                )
                print(f"  [{index}] {title} -- max: {max_score}")

def cmd_task(
    cookies: tuple[str, str],
    bearer: str,
    org: str,
    comp: str,
    task_id: str,
    display_id: str | None = None,
) -> int:
    try:
        payload = load_task_view(cookies, bearer, org, comp, task_id)
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1
    print_task(display_id or task_id, payload["task"])
    return 0

def load_task_file_categories(
    cookies: tuple[str, str], bearer: str, org: str, comp: str, task_id: str
) -> list[str]:
    categories: list[str] = []
    if task_has_statement(cookies, bearer, org, comp, task_id):
        categories.append("statement")

    for category in load_task_file_links(cookies, org, comp, task_id):
        if category not in categories:
            categories.append(category)
    if len(categories) > (1 if "statement" in categories else 0):
        return categories

    status, body, _ = api_request_text(
        path=f"/organization/{org}/competition/{comp}/task/{task_id}/contestantFiles",
        bearer=bearer,
    )
    if status == 200:
        parsed = body_json(body)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str):
                    try:
                        category = normalize_task_file_category(item)
                    except ValueError:
                        continue
                    if category not in categories:
                        categories.append(category)
            return categories

    status, body, _ = request_text(
        path=f"/competitions/{org}/{comp}/{task_id}/view",
        cookies=cookies,
        timeout=30,
    )
    if status == 200:
        for category, label in TASK_FILE_PAGE_LABELS.items():
            if label in body and category not in categories:
                categories.append(category)

    return categories

def load_task_file_links(
    cookies: tuple[str, str], org: str, comp: str, task_id: str
) -> dict[str, str]:
    status, body, _ = request_text(
        path=f"/competitions/{org}/{comp}/{task_id}/view",
        cookies=cookies,
        timeout=30,
    )
    if status != 200:
        return {}
    parser = TaskFileLinkParser(org, comp, task_id)
    parser.feed(body)
    return parser.links

def get_task_data_options(
    cookies: tuple[str, str], bearer: str, org: str, comp: str, task_id: str
) -> list[dict[str, Any]]:
    available = set(load_task_file_categories(cookies, bearer, org, comp, task_id))
    return [
        {
            "category": category,
            "label": label,
            "available": category in available,
        }
        for category, label in TASK_FILE_CATEGORIES.items()
    ]

def download_task_file(
    cookies: tuple[str, str],
    bearer: str,
    org: str,
    comp: str,
    task_id: str,
    category: str,
    links: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    category = normalize_task_file_category(category)
    link = (links or {}).get(category)
    if link:
        return request(path=request_path_from_href(link), cookies=cookies, timeout=180)

    status, body, headers = api_request_bytes(
        path=f"/organization/{org}/competition/{comp}/task/{task_id}/file",
        bearer=bearer,
        params={"file_category": category},
        timeout=180,
    )
    if status == 200 and not response_is_html(body, headers):
        return status, body, headers

    return request(
        path=f"/competitions/{org}/{comp}/{task_id}/{category}/download",
        cookies=cookies,
        timeout=180,
    )

def write_task_file(
    body: bytes,
    headers: dict[str, str],
    category: str,
    output_path: str | None,
    output_dir: str,
    *,
    force: bool = False,
) -> str:
    if output_path:
        target = output_path
    else:
        target = os.path.join(output_dir, task_file_name(category, headers, body))

    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(target) and not force:
        raise RuntimeError(f"Refusing to overwrite existing file: {target}")

    with open(target, "wb") as f:
        f.write(body)
    return target

def stream_task_file(
    cookies: tuple[str, str],
    bearer: str,
    org: str,
    comp: str,
    task_id: str,
    *,
    categories: list[str] | None,
    force: bool = False,
) -> None:
    """Write one task file verbatim to stdout without creating local files."""
    if not categories or len(categories) != 1:
        raise RuntimeError("--output - requires exactly one --category")
    if bool(getattr(sys.stdout, "isatty", lambda: False)()) and not force:
        raise RuntimeError(
            "Refusing to write task data to an interactive terminal; use --force"
        )

    category = normalize_task_file_category(categories[0])
    if category == "statement":
        body = task_statement_markdown(cookies, bearer, org, comp, task_id)
    else:
        links = load_task_file_links(cookies, org, comp, task_id)
        status, body, headers = download_task_file(
            cookies, bearer, org, comp, task_id, category, links
        )
        if status != 200 or response_is_html(body, headers):
            preview = body.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Could not download {category}: HTTP {status}: {error_preview(preview)}"
            )

    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def extract_task_archive(
    path: str, *, force: bool = False
) -> tuple[str | None, str | None]:
    if not path.lower().endswith(".zip") or not zipfile.is_zipfile(path):
        return None, None

    output_dir = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            total_size = sum(member.file_size for member in members)
            compressed_size = sum(member.compress_size for member in members)
            ratio = total_size / max(compressed_size, 1)
            reasons = []
            if len(members) > ZIP_BOMB_MAX_FILES:
                reasons.append(f"{len(members)} files")
            if total_size > ZIP_BOMB_MAX_UNCOMPRESSED_BYTES:
                reasons.append(f"{total_size} uncompressed bytes")
            if (
                total_size >= ZIP_BOMB_RATIO_MIN_BYTES
                and ratio > ZIP_BOMB_MAX_COMPRESSION_RATIO
            ):
                reasons.append(f"{ratio:.0f}x compression ratio")
            if reasons:
                return None, (
                    f"Possible zip bomb detected in {path} ({', '.join(reasons)}); "
                    "automatic extraction skipped and archive kept"
                )

            for member in members:
                target = os.path.realpath(os.path.join(output_dir, member.filename))
                if os.path.commonpath((output_dir, target)) != output_dir:
                    raise RuntimeError(
                        f"Refusing to extract unsafe archive path: {member.filename}"
                    )
                if os.path.lexists(target) and not member.is_dir() and not force:
                    raise RuntimeError(f"Refusing to overwrite existing file: {target}")
            archive.extractall(output_dir)
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"Could not extract archive: {path}") from e

    os.remove(path)
    return output_dir, None

def download_task_data(
    cookies: tuple[str, str],
    bearer: str,
    org: str,
    comp: str,
    task_id: str,
    *,
    categories: list[str] | None = None,
    output_dir: str = ".",
    output_path: str | None = None,
    force: bool = False,
    show_progress: bool = True,
) -> list[dict[str, Any]]:
    explicit_categories = categories is not None
    normalized_categories = (
        [normalize_task_file_category(category) for category in categories]
        if categories
        else load_task_file_categories(cookies, bearer, org, comp, task_id)
    )
    if output_path and len(normalized_categories) != 1:
        raise RuntimeError("--output can only be used with exactly one --category")
    if not normalized_categories:
        raise RuntimeError("No downloadable task data files found")

    task_file_links = load_task_file_links(cookies, org, comp, task_id)
    results: list[dict[str, Any]] = []
    for category in normalized_categories:
        spinner_stop: threading.Event | None = None
        spinner_thread: threading.Thread | None = None
        if show_progress:
            spinner_stop, spinner_thread = _start_spinner(
                f"Downloading {category} ..."
            )
        try:
            if category == "statement":
                body = task_statement_markdown(cookies, bearer, org, comp, task_id)
                headers: dict[str, str] = {}
            else:
                status, body, headers = download_task_file(
                    cookies, bearer, org, comp, task_id, category, task_file_links
                )
                if status != 200 or response_is_html(body, headers):
                    if not explicit_categories:
                        continue
                    preview = body.decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Could not download {category}: HTTP {status}: {error_preview(preview)}"
                    )
            path = write_task_file(
                body,
                headers,
                category,
                output_path,
                output_dir,
                force=force,
            )
            extracted_to, warning = extract_task_archive(path, force=force)
            result = {
                "category": category,
                "path": extracted_to or path,
                "bytes": len(body),
            }
            if extracted_to:
                result["extracted"] = True
            if warning:
                result["warning"] = warning
            results.append(result)
        finally:
            if spinner_stop is not None and spinner_thread is not None:
                _stop_spinner(spinner_stop, spinner_thread)
    if not results:
        raise RuntimeError("No downloadable task data files found")
    return results

def cmd_download_data(
    cookies: tuple[str, str],
    bearer: str,
    org: str,
    comp: str,
    task_id: str,
    *,
    categories: list[str] | None,
    output_dir: str,
    output_path: str | None,
    force: bool,
    list_only: bool = False,
) -> int:
    if list_only:
        try:
            available = load_task_file_categories(
                cookies, bearer, org, comp, task_id
            )
        except (RuntimeError, ValueError, OSError) as e:
            print(f"Error: {e}")
            return 1
        if not available:
            print("No task data files available")
            return 0
        for category in available:
            try:
                category = normalize_task_file_category(category)
            except ValueError:
                continue
            label = TASK_FILE_CATEGORIES.get(category) or category.replace("_", " ").capitalize()
            print(f"{category}\t{label}")
        return 0

    stream_stdout = output_path == "-"
    try:
        if stream_stdout:
            stream_task_file(
                cookies,
                bearer,
                org,
                comp,
                task_id,
                categories=categories,
                force=force,
            )
            return 0
        results = download_task_data(
            cookies,
            bearer,
            org,
            comp,
            task_id,
            categories=categories,
            output_dir=output_dir,
            output_path=output_path,
            force=force,
        )
    except (RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr if stream_stdout else sys.stdout)
        return 1

    for result in results:
        if result.get("warning"):
            print(f"Warning: {result['warning']}")
        action = "Downloaded and extracted" if result.get("extracted") else "Downloaded"
        print(
            f"{action} {result['category']} -> {result['path']} ({result['bytes']} bytes)"
        )
    return 0

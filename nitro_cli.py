#!/usr/bin/env python3
"""
Nitro AI Judge CLI.

Usage:
    nitro-cli
    nitro-cli [--api-url URL] [--submission-proxy] <command>
    nitro-cli login [--username USER --password PASS]
    nitro-cli contests [--page N] [--page-size N] [--all-pages] [--all]
    nitro-cli tasks <org> <comp>
    nitro-cli tasks <org>/<comp>
    nitro-cli task <org> <comp> <task_id>
    nitro-cli task <org>/<comp> <task_id>
    nitro-cli download-data <org> <comp> <task_id> [--category CATEGORY ...] [--out-dir DIR] [--output FILE] [--force]
    nitro-cli download-data <org>/<comp> <task_id> [--category CATEGORY ...] [--out-dir DIR] [--output FILE] [--force]
    nitro-cli play <org> <comp> [--gpu] [--port PORT]
    nitro-cli play <org>/<comp> [--gpu] [--port PORT]
    nitro-cli play logs <org>/<comp>
    nitro-cli play down <org>/<comp>
    nitro-cli submit <org> <comp> <task_id> --output FILE [--source FILE] [--note TEXT] [--wait]
    nitro-cli submissions <org> <comp> <task_id> [--author USER] [--page N] [--page-size N] [--mode MODE]
    nitro-cli submission <submission_id>
    nitro-cli set-final <submission_id>
    nitro-cli unset-final <submission_id>

"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
from html.parser import HTMLParser
import json
import os
import readline
import shlex
import socket
import subprocess
import sys
import time
import urllib.error as urllib_error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request as urllib_request
import uuid
from typing import Any

BASE_URL = "https://judge.nitro-ai.org"
DEFAULT_API_BASE_URL = f"{BASE_URL}/api"
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def clean_env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def clean_api_url(value: str) -> str:
    return value.strip().rstrip("/")


def env_flag(name: str) -> bool:
    value = clean_env_value(name)
    return value is not None and value.lower() in TRUE_ENV_VALUES


def resolve_api_base_url(api_url: str | None = None) -> tuple[str, bool]:
    if api_url and api_url.strip():
        return clean_api_url(api_url), False

    nitro_api_url = clean_env_value("NITRO_API_BASE_URL")
    if nitro_api_url:
        return clean_api_url(nitro_api_url), False

    proxy_url = clean_env_value("PROXY_URL")
    if proxy_url:
        return clean_api_url(proxy_url), True

    return DEFAULT_API_BASE_URL, False


API_BASE_URL, _PROXY_URL_SELECTED = resolve_api_base_url()
SUBMISSION_PROXY_MODE = env_flag("NITRO_SUBMISSION_PROXY") or _PROXY_URL_SELECTED
UA = "Nitro CLI/0.1"
DEFAULT_PAGE_SIZE = 20
DEFAULT_SUBMISSION_PAGE_SIZE = 10
TASK_FILE_CATEGORIES = {
    "statement": "Statement",
    "train_data": "Train data",
    "test_data": "Test data",
    "sample_output": "Sample output",
    "custom_archive": "Starter kit",
}
DEFAULT_TASK_FILE_CATEGORIES = tuple(TASK_FILE_CATEGORIES)
TASK_FILE_PAGE_LABELS = {
    "train_data": "Train Data",
    "test_data": "Test Data",
    "sample_output": "Sample Output",
    "custom_archive": "Custom Archive",
}


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


STATE_DIR = os.environ.get("NITRO_STATE_DIR", os.path.expanduser("~/.nitro-cli"))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
HISTORY_FILE = os.path.join(STATE_DIR, "history")
PLAY_STATE_DIR = os.path.join(STATE_DIR, "contestant-cloud")
PLAY_NETWORK = "nitro_net"
PLAY_DEFAULT_PORT = 8888
PLAY_PROXY_PORT = 9000


def ensure_state_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


def load_state() -> dict[str, Any] | None:
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def decode_session(session_cookie: str) -> dict[str, Any] | None:
    try:
        return json.loads(base64.b64decode(urllib.parse.unquote(session_cookie)))
    except Exception:
        return None


def encode_session(state: dict[str, Any]) -> str | None:
    access_token = state.get("access_token") or state.get("accessToken") or ""
    refresh_token = state.get("refresh_token") or state.get("refreshToken") or ""
    if not access_token or not refresh_token:
        return None
    payload = {
        "username": state.get("username") or "",
        "role": state.get("role") or "user",
        "accessToken": access_token,
        "refreshToken": refresh_token,
    }
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return urllib.parse.quote(encoded.decode())


def get_auth(state: dict[str, Any]) -> tuple[str | None, str | None, str] | None:
    cf = session = None
    access_token = state.get("access_token") or state.get("accessToken") or ""
    for cookie in state.get("cookies", []):
        if cookie.get("name") == "cf_clearance":
            cf = cookie.get("value")
        elif cookie.get("name") == "Cookie":
            session = cookie.get("value")
    if session:
        decoded = decode_session(session)
        if decoded:
            access_token = decoded.get("accessToken", "")
    else:
        session = encode_session(state)
    if not access_token:
        return None
    return cf, session, access_token


def request(
    path: str,
    cookies: tuple[str, str] | None = None,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    bearer: str = "",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 30,
    base_url: str = BASE_URL,
) -> tuple[int, bytes, dict[str, str]]:
    url = f"{base_url.rstrip('/')}{path}"
    if params:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        if query:
            url += f"?{query}"

    req_headers = {"User-Agent": UA}
    if cookies:
        cookie_parts = []
        if cookies[0]:
            cookie_parts.append(f"cf_clearance={cookies[0]}")
        if cookies[1]:
            cookie_parts.append(f"Cookie={cookies[1]}")
        if cookie_parts:
            req_headers["Cookie"] = "; ".join(cookie_parts)
    if bearer:
        req_headers["Authorization"] = f"Bearer {bearer}"
    if headers:
        req_headers.update(headers)

    try:
        req = urllib_request.Request(url, headers=req_headers, data=data, method=method)
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib_error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body, dict(e.headers.items())
    except Exception as e:
        return 0, str(e).encode("utf-8", errors="replace"), {}


def api_request_bytes(**kwargs: Any) -> tuple[int, bytes, dict[str, str]]:
    return request(base_url=API_BASE_URL, **kwargs)


def request_text(**kwargs: Any) -> tuple[int, str, dict[str, str]]:
    status, body, headers = request(**kwargs)
    return status, body.decode("utf-8", errors="replace"), headers


def api_request_text(**kwargs: Any) -> tuple[int, str, dict[str, str]]:
    return request_text(base_url=API_BASE_URL, **kwargs)


def configure_runtime(api_url: str | None, submission_proxy: bool) -> None:
    global API_BASE_URL, SUBMISSION_PROXY_MODE
    API_BASE_URL, proxy_url_selected = resolve_api_base_url(api_url)
    SUBMISSION_PROXY_MODE = (
        submission_proxy or env_flag("NITRO_SUBMISSION_PROXY") or proxy_url_selected
    )


def parse_singlefetch(body: str) -> dict[str, Any] | list[Any] | None:
    try:
        raw = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(raw, list) or len(raw) < 2:
        return raw

    def resolve(value: Any, depth: int = 0) -> Any:
        if value is None or isinstance(value, (bool, float, str)):
            return value
        if isinstance(value, int):
            if value < 0:
                return None
            if 0 <= value < len(raw):
                target = raw[value]
                if depth < 6 and (
                    isinstance(target, (dict, list, str, bool)) or target is None
                ):
                    return resolve(target, depth + 1)
                return target
            return value
        if isinstance(value, list):
            return [resolve(item, depth) for item in value]
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, child in value.items():
                if isinstance(key, str) and key.startswith("_"):
                    try:
                        index = int(key[1:])
                        field_name = raw[index] if 0 <= index < len(raw) else key
                    except ValueError:
                        field_name = key
                    result[field_name if isinstance(field_name, str) else key] = (
                        resolve(child, depth)
                    )
                else:
                    result[key] = resolve(child, depth)
            return result
        return value

    return resolve(raw)


def build_multipart(
    fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]
) -> tuple[bytes, str]:
    boundary = f"----NitroCli{uuid.uuid4().hex}"
    parts: list[bytes] = []

    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    for name, (filename, content, content_type) in files.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        parts.append(content)
        parts.append(b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def parse_competition_ref(parts: list[str]) -> tuple[str, str]:
    if len(parts) == 1:
        if "/" not in parts[0]:
            raise ValueError("competition must be <org>/<comp> or <org> <comp>")
        org, comp = parts[0].split("/", 1)
        return org, comp
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError("competition must be <org>/<comp> or <org> <comp>")


def play_slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value)


def play_workdir(org: str, comp: str) -> str:
    return os.path.join(PLAY_STATE_DIR, f"{play_slug(org)}-{play_slug(comp)}")


def play_project_name(org: str, comp: str) -> str:
    return f"nitro-{play_slug(org)}-{play_slug(comp)}"


def run_process(
    cmd: list[str], *, cwd: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError("Docker is not installed or not on PATH") from e
    if check and result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(output or f"{cmd[0]} exited with {result.returncode}")
    return result


def ensure_docker_ready() -> None:
    run_process(["docker", "--version"])
    run_process(["docker", "compose", "version"])
    run_process(["docker", "info"])


def ensure_play_network() -> None:
    inspect = run_process(["docker", "network", "inspect", PLAY_NETWORK], check=False)
    if inspect.returncode != 0:
        run_process(["docker", "network", "create", PLAY_NETWORK])


def remove_play_network_if_unused() -> None:
    result = run_process(["docker", "network", "rm", PLAY_NETWORK], check=False)
    if result.returncode == 0:
        return
    output = f"{result.stderr}\n{result.stdout}"
    ignored = ("No such network", "has active endpoints", "active endpoints", "in use")
    if not any(text in output for text in ignored):
        raise RuntimeError(output.strip() or f"Could not remove {PLAY_NETWORK}")


def play_compose_base_cmd(org: str, comp: str) -> list[str]:
    workdir = play_workdir(org, comp)
    return [
        "docker",
        "compose",
        "--project-name",
        play_project_name(org, comp),
        "--file",
        os.path.join(workdir, "docker-compose.yml"),
    ]


def play_jupyter_running(org: str, comp: str) -> bool:
    compose_file = os.path.join(play_workdir(org, comp), "docker-compose.yml")
    if not os.path.exists(compose_file):
        return False
    result = run_process(
        [*play_compose_base_cmd(org, comp), "ps", "--status", "running", "--services"],
        cwd=play_workdir(org, comp),
        check=False,
    )
    return result.returncode == 0 and "jupyter-server" in result.stdout.splitlines()


def ensure_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(
                f"Host port {port} is already in use; rerun with --port N"
            )


def write_play_files(org: str, comp: str, port: int, gpu: bool) -> str:
    workdir = play_workdir(org, comp)
    secrets_dir = os.path.join(workdir, "secrets")
    os.makedirs(secrets_dir, exist_ok=True)
    secret_path = os.path.join(secrets_dir, "session_whitelist_bypass_key")
    if not os.path.exists(secret_path):
        open(secret_path, "a", encoding="utf-8").close()

    env = "\n".join(
        [
            "JUDGE_BASE_URL=https://judge.nitro-ai.org/api",
            f"ORGANIZATION_SLUG={org}",
            f"COMPETITION_SLUG={comp}",
            f"JUPYTER_PORT={port}",
            f"PROXY_PORT={PLAY_PROXY_PORT}",
            "JUPYTER_BASE_URL=http://jupyter-server:8888/",
            "SESSION_WHITELIST_BYPASS_KEY_FILE=/run/secrets/session_whitelist_bypass_key",
            "PROXY_DISABLE_CACHE=1",
            "PROXY_PREJUDGING_TIMEOUT_S=120",
            "",
        ]
    )
    with open(os.path.join(workdir, ".env"), "w", encoding="utf-8") as f:
        f.write(env)

    gpu_block = ""
    if gpu:
        gpu_block = """    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
"""
    compose = f"""services:
  jupyter-server:
    image: nitroai/{org}-{comp}-notebook:latest
    ports:
      - "${{JUPYTER_PORT:-8888}}:8888"
    environment:
      PROXY_URL: "http://submission-proxy:${{PROXY_PORT:-9000}}"
      PROXY_URL_CLIENT: "http://localhost:${{PROXY_PORT:-9000}}"
    cap_add:
      - NET_ADMIN
    networks:
      - {PLAY_NETWORK}
    depends_on:
      - submission-proxy
{gpu_block}  submission-proxy:
    image: nitroai/{org}-{comp}-judge-proxy:latest
    ports:
      - "${{PROXY_PORT:-9000}}:9000"
    environment:
      JUDGE_BASE_URL: "${{JUDGE_BASE_URL}}"
      ORGANIZATION_SLUG: "${{ORGANIZATION_SLUG}}"
      COMPETITION_SLUG: "${{COMPETITION_SLUG}}"
      PROXY_PORT: "${{PROXY_PORT:-9000}}"
      JUPYTER_BASE_URL: "http://jupyter-server:8888/"
      SESSION_WHITELIST_BYPASS_KEY_FILE: "/run/secrets/session_whitelist_bypass_key"
      PROXY_DISABLE_CACHE: "1"
      PROXY_PREJUDGING_TIMEOUT_S: "120"
    secrets:
      - session_whitelist_bypass_key
    networks:
      - {PLAY_NETWORK}

networks:
  {PLAY_NETWORK}:
    external: true

secrets:
  session_whitelist_bypass_key:
    file: ./secrets/session_whitelist_bypass_key
"""
    with open(os.path.join(workdir, "docker-compose.yml"), "w", encoding="utf-8") as f:
        f.write(compose)
    return workdir


def cmd_play_up(org: str, comp: str, *, gpu: bool, port: int) -> int:
    ensure_docker_ready()
    workdir = write_play_files(org, comp, port, gpu)
    if not play_jupyter_running(org, comp):
        ensure_port_free(port)
    ensure_play_network()
    base_cmd = play_compose_base_cmd(org, comp)
    run_process([*base_cmd, "pull"], cwd=workdir)
    run_process([*base_cmd, "up", "-d"], cwd=workdir)
    print(f"Started {org}/{comp}")
    print(f"Jupyter: http://localhost:{port}")
    print(f"State: {workdir}")
    return 0


def cmd_play_down(org: str, comp: str) -> int:
    ensure_docker_ready()
    workdir = play_workdir(org, comp)
    if not os.path.exists(os.path.join(workdir, "docker-compose.yml")):
        print(f"No play environment found: {workdir}")
        return 0
    run_process([*play_compose_base_cmd(org, comp), "down"], cwd=workdir)
    remove_play_network_if_unused()
    print(f"Stopped {org}/{comp}")
    return 0


def cmd_play_logs(org: str, comp: str) -> int:
    ensure_docker_ready()
    workdir = play_workdir(org, comp)
    if not os.path.exists(os.path.join(workdir, "docker-compose.yml")):
        print(f"No play environment found: {workdir}")
        return 1
    try:
        result = subprocess.run(
            [*play_compose_base_cmd(org, comp), "logs", "-f"],
            cwd=workdir,
        )
    except FileNotFoundError:
        print("Error: Docker is not installed or not on PATH")
        return 1
    return result.returncode


def cmd_play(args: argparse.Namespace) -> int:
    parts = list(args.play_args)
    if not parts:
        print("Error: play requires <org>/<comp> or down/logs <org>/<comp>")
        return 1
    action = "up"
    if parts[0] in {"down", "logs"}:
        action = parts.pop(0)
    try:
        org, comp = parse_competition_ref(parts)
        if action == "down":
            return cmd_play_down(org, comp)
        if action == "logs":
            return cmd_play_logs(org, comp)
        return cmd_play_up(org, comp, gpu=args.gpu, port=args.port)
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}")
        return 1


def format_datetime_ms(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value / 1000))


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


def body_json(body: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def list_payload(data: Any, *keys: str) -> list[Any] | None:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
        nested = list_payload(value, *keys)
        if nested is not None:
            return nested
    return None


def int_payload(data: Any, *keys: str, default: int = 1) -> int:
    if not isinstance(data, dict):
        return default
    for key in keys:
        value = data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    nested = data.get("data")
    if isinstance(nested, dict):
        return int_payload(nested, *keys, default=default)
    return default


def error_preview(body: str) -> str:
    preview = body.strip()
    return preview[:300] if preview else ""


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
    if normalized not in TASK_FILE_CATEGORIES:
        valid = ", ".join(TASK_FILE_CATEGORIES)
        raise ValueError(f"invalid file category '{category}'; valid categories: {valid}")
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
    filename = os.path.basename(filename.strip().strip('"'))
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
    return "text/html" in content_type or body.lstrip().lower().startswith(b"<!doctype html")


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


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def token_is_expired(token: str, buffer_seconds: int = 60) -> bool:
    payload = decode_jwt_payload(token) or {}
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return exp - time.time() <= buffer_seconds


def normalize_tokens(
    tokens: dict[str, Any], username: str | None = None
) -> dict[str, Any]:
    access_token = tokens.get("access_token") or tokens.get("accessToken") or ""
    refresh_token = tokens.get("refresh_token") or tokens.get("refreshToken") or ""
    access_payload = decode_jwt_payload(access_token) or {}
    refresh_payload = decode_jwt_payload(refresh_token) or {}
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_token_exp": access_payload.get("exp"),
        "refresh_token_exp": refresh_payload.get("exp"),
        "username": tokens.get("username")
        or username
        or access_payload.get("username")
        or "",
        "role": tokens.get("role") or access_payload.get("role"),
    }


def save_token_state(tokens: dict[str, Any], username: str | None = None) -> None:
    ensure_state_dir()
    state = normalize_tokens(tokens, username)
    state["timestamp"] = time.time()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def refresh_saved_tokens(state: dict[str, Any]) -> dict[str, Any] | None:
    refresh_token = state.get("refresh_token") or state.get("refreshToken")
    if not refresh_token:
        for cookie in state.get("cookies", []):
            if cookie.get("name") == "Cookie":
                decoded = decode_session(cookie.get("value", "")) or {}
                refresh_token = decoded.get("refreshToken")
                break
    if not refresh_token:
        return None
    form_data = urllib.parse.urlencode({"refreshToken": refresh_token}).encode("utf-8")
    status, body, _ = api_request_text(
        path="/auth/refreshToken",
        method="POST",
        data=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if status != 200:
        return None
    parsed = body_json(body)
    if not isinstance(parsed, dict):
        return None
    refreshed = normalize_tokens(parsed, state.get("username"))
    save_token_state(refreshed, refreshed.get("username"))
    return load_state()


def ensure_fresh_state(state: dict[str, Any]) -> dict[str, Any] | None:
    auth = get_auth(state)
    access_token = auth[2] if auth else ""
    if access_token and not token_is_expired(access_token):
        return state
    return refresh_saved_tokens(state)


def hash_password(password: str) -> str:
    return base64.b64encode(hashlib.sha256(password.encode()).digest()).decode()


def do_login(username: str, password: str) -> dict[str, Any]:
    form_data = urllib.parse.urlencode(
        {"username": username, "password": hash_password(password)}
    ).encode("utf-8")
    status, body, headers = api_request_text(
        path="/auth/login",
        method="POST",
        data=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )

    result: dict[str, Any] = {
        "success": False,
        "tokens": None,
        "http_code": status,
        "error": None,
    }

    parsed = body_json(body)
    if (
        status == 200
        and isinstance(parsed, dict)
        and (parsed.get("access_token") or parsed.get("accessToken"))
    ):
        result["success"] = True
        result["tokens"] = parsed
        result["username"] = headers.get("x-set-username", username)
        return result

    if isinstance(parsed, dict):
        result["error"] = (
            parsed.get("message") or parsed.get("error") or f"HTTP {status}"
        )
    else:
        result["error"] = f"HTTP {status}: {error_preview(body)}"
    return result


def cmd_login(username: str | None, password: str | None) -> int:
    if not username:
        username = input("Username: ").strip()
        if not username:
            print("Aborted.")
            return 1

    if not password:
        password = getpass.getpass("Password: ")
        if not password:
            print("Aborted.")
            return 1

    result = do_login(username, password)

    if result["success"] and result.get("tokens"):
        save_token_state(result["tokens"], result.get("username"))
        state = load_state() or {}
        print(
            f"Login OK | user={state.get('username')} | role={state.get('role')}"
        )
        return 0

    print(f"Login failed: {result.get('error')}")
    return 1


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
                default=1,
            )
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
    if last_page <= 1:
        return all_competitions

    page_results: dict[int, list[dict[str, Any]]] = {}
    max_workers = min(COMPETITION_PAGE_WORKERS, last_page - 1)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                load_competitions_page,
                cookies,
                bearer,
                page=next_page,
                page_size=page_size,
                featured=featured,
            ): next_page
            for next_page in range(2, last_page + 1)
        }
        for future in as_completed(futures):
            next_page = futures[future]
            page_items, _ = future.result()
            page_results[next_page] = page_items

    for next_page in range(2, last_page + 1):
        all_competitions.extend(page_results.get(next_page, []))
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

    status, body, _ = api_request_text(path="/tasks", bearer=bearer)
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


def print_tasks(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        print("No tasks found")
        return
    for task in tasks:
        task_id = task.get("id") or "?"
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
    cookies: tuple[str, str], bearer: str, org: str, comp: str, task_id: str
) -> int:
    try:
        payload = load_task_view(cookies, bearer, org, comp, task_id)
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1
    print_task(task_id, payload["task"])
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
        results.append({"category": category, "path": path, "bytes": len(body)})
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
) -> int:
    try:
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
        print(f"Error: {e}")
        return 1

    for result in results:
        print(
            f"Downloaded {result['category']} -> {result['path']} ({result['bytes']} bytes)"
        )
    return 0


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
    note = note.strip() or "nitro-cli"
    if SUBMISSION_PROXY_MODE:
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
            timeout=120,
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
        timeout=120,
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

    author = get_username(load_state()) or None

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


def shell_help() -> None:
    print(
        """Commands:
  help
  exit | quit
  back | unselect
  login [username] [password]
  status
  contests
  contest list [--all] [--all-pages] [--page N] [--page-size N]
  contest select <index|org/slug>
  contest show
  tasks
  task list
  task select <index|id>
  select <index|id>
  show
  submit <output.csv> [source.py] [--note TEXT] [--wait]
  task show
  task download-data [--category CATEGORY ...] [--out-dir DIR] [--force]
  task submit <output.csv> [source.py] [--note TEXT] [--wait]
  submissions [--mode partial|complete|both]
  task submissions list [--mode partial|complete|both]
  submission <index|short-id|full-id>
  submission view <index|short-id|full-id>
  task submissions show <index|short-id|full-id>
  set-final <index|short-id|full-id>
  unset-final <index|short-id|full-id>

Notes:
  select is context-sensitive:
    - at top level, it selects a contest
    - inside a contest, it selects a task
  back/unselect clears the current task first, then the current contest
"""
    )


def shell_prompt(ctx: dict[str, Any]) -> str:
    contest = ctx.get("contest") or {}
    task = ctx.get("task") or {}
    parts = ["nitro"]
    if contest:
        parts.append(
            f"{contest.get('organizationSlug')}/{contest.get('competitionSlug')}"
        )
    if task:
        parts.append(f"task:{task.get('id')}")
    return "[" + " | ".join(parts) + "]> "


def setup_readline(ctx: dict[str, Any]) -> None:
    ensure_state_dir()
    try:
        readline.read_history_file(HISTORY_FILE)
    except FileNotFoundError:
        pass
    except OSError:
        pass

    readline.set_history_length(1000)
    readline.parse_and_bind("tab: complete")
    readline.parse_and_bind("set show-all-if-ambiguous on")

    def completer(text: str, state: int) -> str | None:
        buffer = readline.get_line_buffer()
        parts = shlex.split(buffer) if buffer.strip() else []
        contests = ctx.get("contests") or []
        tasks = ctx.get("tasks") or []
        submissions = ctx.get("submission_items") or []

        candidates = [
            "help",
            "exit",
            "quit",
            "back",
            "login",
            "status",
            "contests",
            "contest",
            "tasks",
            "task",
            "select",
            "show",
            "submit",
            "download-data",
            "submissions",
            "submission",
            "set-final",
            "unset-final",
        ]
        if parts[:1] == ["contest"]:
            candidates = ["list", "select", "show"]
        elif parts[:1] == ["submission"]:
            candidates = ["view"]
        elif parts[:2] == ["contest", "list"]:
            candidates = ["--all", "--all-pages", "--page", "--page-size"]
        elif parts[:2] == ["contest", "select"]:
            candidates = [
                *[str(i) for i in range(1, len(contests) + 1)],
                *[
                    f"{item.get('organizationSlug')}/{item.get('competitionSlug')}"
                    for item in contests
                ],
            ]
        elif parts[:1] == ["task"]:
            candidates = ["list", "select", "show", "download-data", "submit", "submissions"]
        elif parts[:1] == ["select"]:
            candidates = (
                [str(i) for i in range(1, len(tasks) + 1)]
                if ctx.get("contest")
                else [str(i) for i in range(1, len(contests) + 1)]
            )
        elif parts[:2] == ["task", "select"]:
            candidates = [
                *[str(i) for i in range(1, len(tasks) + 1)],
                *[str(item.get("id")) for item in tasks],
            ]
        elif parts[:1] == ["submit"]:
            candidates = ["--note", "--wait"]
        elif parts[:1] == ["submissions"]:
            candidates = ["--mode", "partial", "complete", "both"]
        elif parts[:1] in (["submission"], ["set-final"], ["unset-final"]):
            candidates = [
                *[str(i) for i in range(1, len(submissions) + 1)],
                *[str(item.get("id", "")).split("-")[-1] for item in submissions],
            ]
        elif parts[:2] == ["task", "submit"]:
            candidates = ["--note", "--wait"]
        elif parts[:2] in (["task", "download-data"], ["download-data"]):
            candidates = [
                "--category",
                "--out-dir",
                "--force",
                *DEFAULT_TASK_FILE_CATEGORIES,
            ]
        elif parts[:3] == ["task", "submissions", "list"]:
            candidates = ["--mode", "partial", "complete", "both"]
        elif parts[:3] == ["task", "submissions", "show"]:
            candidates = [
                *[str(i) for i in range(1, len(submissions) + 1)],
                *[str(item.get("id", "")).split("-")[-1] for item in submissions],
            ]

        matches = [candidate for candidate in candidates if candidate.startswith(text)]
        return matches[state] if state < len(matches) else None

    readline.set_completer(completer)


def save_shell_history() -> None:
    ensure_state_dir()
    try:
        readline.write_history_file(HISTORY_FILE)
    except OSError:
        pass


def shell_ensure_auth() -> tuple[dict[str, Any], tuple[str, str], str] | None:
    auth_data = require_auth()
    if auth_data:
        return auth_data
    else:
        print("Login required.")
    if cmd_login(None, None) != 0:
        return None
    auth_data = require_auth()
    if auth_data:
        return auth_data
    return None


def shell_select_contest(
    token: str, ctx: dict[str, Any], cookies: tuple[str, str]
) -> tuple[bool, str]:
    contests = ctx.get("contests") or []
    selected = None
    if token.isdigit():
        index = int(token) - 1
        if 0 <= index < len(contests):
            selected = contests[index]
    else:
        for contest in contests:
            ref = f"{contest.get('organizationSlug')}/{contest.get('competitionSlug')}"
            if token == ref:
                selected = contest
                break
    if not selected:
        return False, "Contest not found"

    ctx["contest"] = selected
    ctx["task"] = None
    ctx["tasks"] = load_tasks(
        cookies,
        ctx["bearer"],
        selected.get("organizationSlug"),
        selected.get("competitionSlug"),
    )
    ctx["submission_items"] = []
    task_count = len(ctx["tasks"])
    return (
        True,
        "Selected "
        f"{selected.get('organizationSlug')}/{selected.get('competitionSlug')} "
        f"({task_count} task{'s' if task_count != 1 else ''} loaded). "
        "Use 'tasks' to list them or 'select <task>' to choose one.",
    )


def shell_select_task(token: str, ctx: dict[str, Any]) -> tuple[bool, str]:
    tasks = ctx.get("tasks") or []
    if not ctx.get("contest"):
        return (
            False,
            "No contest selected. Use 'contests' or 'contest select ...' first.",
        )
    selected = None
    if token.isdigit():
        index = int(token) - 1
        if 0 <= index < len(tasks):
            selected = tasks[index]
        else:
            for task in tasks:
                if str(task.get("id")) == token:
                    selected = task
                    break
    else:
        for task in tasks:
            if str(task.get("id")) == token:
                selected = task
                break
    if not selected:
        if token.isdigit() and ctx.get("contest"):
            return (
                False,
                "Task not found. You're inside a contest, so 'select' expects a task. "
                "Use 'tasks' to list tasks, or 'contest select ...' / 'back' to change contests.",
            )
        return False, "Task not found"
    ctx["task"] = selected
    ctx["submission_items"] = []
    return True, f"Selected task {selected.get('id')}: {selected.get('title')}"


def shell_list_contests(
    ctx: dict[str, Any],
    cookies: tuple[str, str],
    bearer: str,
    all_contests: bool,
    *,
    all_pages: bool = False,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> None:
    contests = load_competitions(
        cookies,
        bearer,
        page=page,
        page_size=page_size,
        featured=None if all_contests else True,
        all_pages=all_pages,
    )
    ctx["contests"] = contests
    contest_indexes = {id(contest): index for index, contest in enumerate(contests, 1)}
    for phase, phase_contests in grouped_competitions(contests):
        print(f"{phase}:")
        for contest in phase_contests:
            print(
                f"{contest_indexes[id(contest)]}. [{contest.get('organizationSlug')}/{contest.get('competitionSlug')}] {contest.get('title')}"
            )
        print()


def shell_list_tasks(ctx: dict[str, Any]) -> None:
    tasks = ctx.get("tasks") or []
    if not tasks:
        if ctx.get("contest"):
            print("No tasks available for the selected contest.")
        else:
            print("No tasks loaded. Use 'contest select ...' first.")
        return
    for index, task in enumerate(tasks, 1):
        print(f"{index}. [{task.get('id')}] {task.get('title')}")


def shell_list_submissions(ctx: dict[str, Any], mode: str) -> None:
    contest = ctx.get("contest") or {}
    task = ctx.get("task") or {}
    if not contest or not task:
        print("Select a contest and task first.")
        return
    items, _ = load_submissions(
        ctx["cookies"],
        ctx["bearer"],
        contest.get("organizationSlug"),
        contest.get("competitionSlug"),
        str(task.get("id")),
        author=get_username(ctx.get("state")),
        page=None,
        page_size=DEFAULT_SUBMISSION_PAGE_SIZE,
        mode="partial" if mode == "both" else mode,
    )
    ctx["submission_items"] = items
    if mode == "both":
        cmd_submissions(
            ctx["cookies"],
            ctx["bearer"],
            contest.get("organizationSlug"),
            contest.get("competitionSlug"),
            str(task.get("id")),
            author=get_username(ctx.get("state")),
            page=None,
            page_size=DEFAULT_SUBMISSION_PAGE_SIZE,
            mode=mode,
        )
        return
    for index, submission in enumerate(items, 1):
        short_id = str(submission.get("id", "")).split("-")[-1]
        print(
            f"{index}. [{short_id}] {format_datetime_ms(submission.get('timestamp'))} | {submission_score(submission, mode)} | {submission.get('state')}"
        )


def shell_load_submission_items(ctx: dict[str, Any]) -> None:
    contest = ctx.get("contest") or {}
    task = ctx.get("task") or {}
    if not contest or not task:
        return
    items, _ = load_submissions(
        ctx["cookies"],
        ctx["bearer"],
        contest.get("organizationSlug"),
        contest.get("competitionSlug"),
        str(task.get("id")),
        author=get_username(ctx.get("state")),
        page=None,
        page_size=DEFAULT_SUBMISSION_PAGE_SIZE,
        mode="partial",
    )
    ctx["submission_items"] = items


def shell_submission_id(token: str, ctx: dict[str, Any]) -> str:
    if token.isdigit() and not (ctx.get("submission_items") or []):
        shell_load_submission_items(ctx)
    items = ctx.get("submission_items") or []
    if token.isdigit():
        index = int(token) - 1
        if 0 <= index < len(items):
            return str(items[index].get("id"))
    return token


def shell_show(ctx: dict[str, Any], cookies: tuple[str, str]) -> None:
    contest = ctx.get("contest")
    task = ctx.get("task")
    if contest and task:
        cmd_task(
            cookies,
            ctx["bearer"],
            contest.get("organizationSlug"),
            contest.get("competitionSlug"),
            str(task.get("id")),
        )
        return
    if contest:
        print(
            f"[{contest.get('organizationSlug')}/{contest.get('competitionSlug')}] {contest.get('title')}"
        )
        print(
            f"{format_datetime_ms(contest.get('competitionStart'))} -> {format_datetime_ms(contest.get('competitionEnd'))}"
        )
        return
    print("No contest selected")


def shell_show_contest(ctx: dict[str, Any]) -> None:
    contest = ctx.get("contest")
    if not contest:
        print("No contest selected")
        return
    print(
        f"[{contest.get('organizationSlug')}/{contest.get('competitionSlug')}] {contest.get('title')}"
    )
    print(
        f"{format_datetime_ms(contest.get('competitionStart'))} -> {format_datetime_ms(contest.get('competitionEnd'))}"
    )


def shell_show_task(ctx: dict[str, Any], cookies: tuple[str, str]) -> None:
    contest = ctx.get("contest")
    task = ctx.get("task")
    if not contest:
        print("No contest selected")
        return
    if not task:
        print("No task selected")
        return
    cmd_task(
        cookies,
        ctx["bearer"],
        contest.get("organizationSlug"),
        contest.get("competitionSlug"),
        str(task.get("id")),
    )


def shell_back(ctx: dict[str, Any]) -> None:
    if ctx.get("task"):
        ctx["task"] = None
        ctx["submission_items"] = []
        print("Returned to contest context")
        return
    if ctx.get("contest"):
        ctx["contest"] = None
        ctx["tasks"] = []
        ctx["submission_items"] = []
        print("Returned to top level")
        return
    print("Already at top level")


def run_shell() -> int:
    auth_data = shell_ensure_auth()
    if not auth_data:
        return 1
    state, cookies, bearer = auth_data
    ctx: dict[str, Any] = {
        "state": state,
        "cookies": cookies,
        "bearer": bearer,
        "contests": [],
        "contest": None,
        "tasks": [],
        "task": None,
        "submission_items": [],
    }

    setup_readline(ctx)
    print("Nitro CLI shell. Type 'help' for commands.")
    while True:
        try:
            line = input(shell_prompt(ctx)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            save_shell_history()
            return 0
        if not line:
            continue
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"Error: {e}")
            continue

        try:
            if parts[0] in {"exit", "quit"}:
                save_shell_history()
                return 0
            if parts[0] in {"back", "unselect"}:
                shell_back(ctx)
                continue
            if parts[0] == "help":
                shell_help()
                continue
            if parts[0] == "status":
                contest = ctx.get("contest")
                task = ctx.get("task")
                print(f"User: {get_username(ctx.get('state')) or '?'}")
                print(
                    "Contest: "
                    + (
                        f"{contest.get('organizationSlug')}/{contest.get('competitionSlug')}"
                        if contest
                        else "none"
                    )
                )
                print(f"Task: {task.get('id') if task else 'none'}")
                continue
            if parts[0] == "login":
                username = parts[1] if len(parts) > 1 else None
                password = parts[2] if len(parts) > 2 else None
                if cmd_login(username, password) == 0:
                    auth_data = require_auth()
                    if auth_data:
                        state, cookies, bearer = auth_data
                        ctx.update(
                            {"state": state, "cookies": cookies, "bearer": bearer}
                        )
                continue
            if parts[0] == "contests":
                shell_list_contests(ctx, cookies, ctx["bearer"], False, page=1)
                continue
            if parts[:2] == ["contest", "list"]:
                page = 1
                page_size = DEFAULT_PAGE_SIZE
                all_pages = "--all-pages" in parts[2:]
                if "--page" in parts[2:]:
                    page_index = parts.index("--page")
                    if page_index + 1 < len(parts):
                        page = int(parts[page_index + 1])
                if "--page-size" in parts[2:]:
                    size_index = parts.index("--page-size")
                    if size_index + 1 < len(parts):
                        page_size = int(parts[size_index + 1])
                shell_list_contests(
                    ctx,
                    cookies,
                    ctx["bearer"],
                    "--all" in parts[2:],
                    all_pages=all_pages,
                    page=page,
                    page_size=page_size,
                )
                continue
            if parts[:2] == ["contest", "select"] and len(parts) >= 3:
                if not ctx.get("contests"):
                    shell_list_contests(ctx, cookies, ctx["bearer"], False, page=1)
                ok, message = shell_select_contest(parts[2], ctx, cookies)
                print(message)
                continue
            if parts[0] == "select" and len(parts) >= 2:
                if ctx.get("contest"):
                    ok, message = shell_select_task(parts[1], ctx)
                else:
                    if not ctx.get("contests"):
                        shell_list_contests(ctx, cookies, ctx["bearer"], False, page=1)
                    ok, message = shell_select_contest(parts[1], ctx, cookies)
                print(message)
                continue
            if parts[:2] == ["contest", "show"]:
                shell_show_contest(ctx)
                continue
            if parts[0] == "tasks":
                shell_list_tasks(ctx)
                continue
            if parts[:2] == ["task", "list"]:
                shell_list_tasks(ctx)
                continue
            if parts[:2] == ["task", "select"] and len(parts) >= 3:
                ok, message = shell_select_task(parts[2], ctx)
                print(message)
                continue
            if parts[0] == "show":
                shell_show(ctx, cookies)
                continue
            if parts[:2] == ["task", "show"]:
                shell_show_task(ctx, cookies)
                continue
            if parts[0] == "download-data":
                parts = ["task", "download-data", *parts[1:]]
            if parts[:2] == ["task", "download-data"]:
                contest = ctx.get("contest")
                task = ctx.get("task")
                if not contest or not task:
                    print("Select a contest and task first.")
                    continue
                categories: list[str] = []
                output_dir = "."
                force = "--force" in parts[2:]
                index = 2
                while index < len(parts):
                    if parts[index] == "--category" and index + 1 < len(parts):
                        categories.append(parts[index + 1])
                        index += 2
                        continue
                    if parts[index] == "--out-dir" and index + 1 < len(parts):
                        output_dir = parts[index + 1]
                        index += 2
                        continue
                    index += 1
                cmd_download_data(
                    cookies,
                    bearer,
                    contest.get("organizationSlug"),
                    contest.get("competitionSlug"),
                    str(task.get("id")),
                    categories=categories or None,
                    output_dir=output_dir,
                    output_path=None,
                    force=force,
                )
                continue
            if parts[0] == "submit" and len(parts) >= 2:
                parts = ["task", "submit", *parts[1:]]
            if parts[:2] == ["task", "submit"] and len(parts) >= 3:
                contest = ctx.get("contest")
                task = ctx.get("task")
                if not contest or not task:
                    print("Select a contest and task first.")
                    continue
                output_path = parts[2]
                source_path = (
                    parts[3]
                    if len(parts) >= 4 and not parts[3].startswith("--")
                    else None
                )
                note = ""
                wait = "--wait" in parts[3:]
                if "--note" in parts[3:]:
                    note_index = parts.index("--note")
                    if note_index + 1 < len(parts):
                        note = parts[note_index + 1]
                cmd_submit(
                    cookies,
                    bearer,
                    contest.get("organizationSlug"),
                    contest.get("competitionSlug"),
                    str(task.get("id")),
                    output_path,
                    source_path,
                    note,
                    wait,
                )
                continue
            if parts[0] == "submissions":
                mode = "both"
                if "--mode" in parts[1:]:
                    mode_index = parts.index("--mode")
                    if mode_index + 1 < len(parts):
                        mode = parts[mode_index + 1]
                shell_list_submissions(ctx, mode)
                continue
            if parts[:3] == ["task", "submissions", "list"]:
                mode = "both"
                if "--mode" in parts[3:]:
                    mode_index = parts.index("--mode")
                    if mode_index + 1 < len(parts):
                        mode = parts[mode_index + 1]
                shell_list_submissions(ctx, mode)
                continue
            if parts[0] == "submission" and len(parts) == 2:
                parts = ["task", "submissions", "show", parts[1]]
            if parts[:2] == ["submission", "view"] and len(parts) >= 3:
                parts = ["task", "submissions", "show", parts[2]]
            if parts[:3] == ["task", "submissions", "show"] and len(parts) >= 4:
                contest = ctx.get("contest")
                task = ctx.get("task")
                if not contest or not task:
                    print("Select a contest and task first.")
                    continue
                cmd_submission(
                    cookies,
                    bearer,
                    shell_submission_id(parts[3], ctx),
                    org=contest.get("organizationSlug"),
                    comp=contest.get("competitionSlug"),
                    task_id=str(task.get("id")),
                )
                continue
            if parts[0] in {"set-final", "unset-final"} and len(parts) >= 2:
                contest = ctx.get("contest")
                task = ctx.get("task")
                if not contest or not task:
                    print("Select a contest and task first.")
                    continue
                cmd_set_final(
                    cookies,
                    bearer,
                    shell_submission_id(parts[1], ctx),
                    parts[0] == "set-final",
                    org=contest.get("organizationSlug"),
                    comp=contest.get("competitionSlug"),
                    task_id=str(task.get("id")),
                )
                continue
            print("Unknown command. Type 'help'.")
        except RuntimeError as e:
            print(f"Error: {e}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Nitro AI Judge CLI")
    parser.add_argument(
        "--api-url",
        help=(
            "Backend API base URL (default: NITRO_API_BASE_URL, then PROXY_URL, "
            "then judge.nitro-ai.org/api)"
        ),
    )
    parser.add_argument(
        "--submission-proxy",
        action="store_true",
        help=(
            "Submit through the Contestant Cloud submission proxy "
            "(also enabled when PROXY_URL is used as the API URL)"
        ),
    )
    sub = parser.add_subparsers(dest="cmd")

    p_login = sub.add_parser("login", help="Login to Nitro Judge")
    p_login.add_argument("--username")
    p_login.add_argument("--password")

    p_contests = sub.add_parser("contests", help="List competitions")
    p_contests.add_argument("--page", type=int, default=1)
    p_contests.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    p_contests.add_argument("--all-pages", action="store_true")
    p_contests.add_argument(
        "--all", action="store_true", help="Include non-featured competitions"
    )

    p_tasks = sub.add_parser("tasks", help="List tasks in a competition")
    p_tasks.add_argument("competition", nargs="+", help="<org>/<comp> or <org> <comp>")

    p_task = sub.add_parser("task", help="Get task details")
    p_task.add_argument("competition", nargs="+", help="<org>/<comp> or <org> <comp>")
    p_task.add_argument("task_id")

    p_download = sub.add_parser("download-data", help="Download task data files")
    p_download.add_argument(
        "competition", nargs="+", help="<org>/<comp> or <org> <comp>"
    )
    p_download.add_argument("task_id")
    p_download.add_argument(
        "--category",
        action="append",
        choices=sorted(TASK_FILE_CATEGORIES),
        help="Task file category to download; repeat for multiple categories",
    )
    p_download.add_argument("--out-dir", default=".")
    p_download.add_argument("--output", help="Output file path for a single category")
    p_download.add_argument("--force", action="store_true", help="Overwrite files")

    p_play = sub.add_parser("play", help="Launch a past contest locally with Docker")
    p_play.add_argument("play_args", nargs="*")
    p_play.add_argument("--gpu", action="store_true", help="Request NVIDIA GPU access")
    p_play.add_argument("--port", type=int, default=PLAY_DEFAULT_PORT)

    p_submit = sub.add_parser("submit", help="Create a submission")
    p_submit.add_argument("competition", nargs="+", help="<org>/<comp> or <org> <comp>")
    p_submit.add_argument("task_id")
    p_submit.add_argument("--output", required=True)
    p_submit.add_argument("--source")
    p_submit.add_argument("--note", default="")
    p_submit.add_argument("--wait", action="store_true")

    p_submissions = sub.add_parser("submissions", help="List submissions for a task")
    p_submissions.add_argument(
        "competition", nargs="+", help="<org>/<comp> or <org> <comp>"
    )
    p_submissions.add_argument("task_id")
    p_submissions.add_argument("--author")
    p_submissions.add_argument("--page", type=int)
    p_submissions.add_argument(
        "--page-size", type=int, default=DEFAULT_SUBMISSION_PAGE_SIZE
    )
    p_submissions.add_argument(
        "--mode", choices=["partial", "complete", "both"], default="both"
    )

    p_submission = sub.add_parser("submission", help="Get submission feedback/details")
    p_submission.add_argument("submission_id")
    p_submission.add_argument("--org")
    p_submission.add_argument("--comp")
    p_submission.add_argument("--task-id")

    p_set_final = sub.add_parser("set-final", help="Mark a submission as final")
    p_set_final.add_argument("submission_id")

    p_unset_final = sub.add_parser("unset-final", help="Unmark a submission as final")
    p_unset_final.add_argument("submission_id")
    return parser


def require_auth() -> tuple[dict[str, Any], tuple[str, str], str] | None:
    state = load_state()
    if not state:
        print("Not logged in. Run: nitro-cli login")
        return None
    state = ensure_fresh_state(state)
    if not state:
        print("Saved login expired. Run: nitro-cli login")
        return None
    auth = get_auth(state)
    if not auth:
        print("Missing access token. Run: nitro-cli login")
        return None
    return state, (auth[0] or "", auth[1] or ""), auth[2]


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_runtime(args.api_url, args.submission_proxy)
    if not args.cmd:
        return run_shell()

    if args.cmd == "login":
        return cmd_login(args.username, args.password)

    if args.cmd == "play":
        return cmd_play(args)

    auth_data = require_auth()
    if not auth_data:
        return 1
    state, cookies, bearer = auth_data

    if args.cmd == "contests":
        featured = None if args.all else True
        return cmd_contests(
            cookies,
            bearer,
            args.page,
            args.page_size,
            featured,
            all_pages=args.all_pages,
        )

    if args.cmd == "tasks":
        try:
            org, comp = parse_competition_ref(args.competition)
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        return cmd_tasks(cookies, bearer, org, comp)

    if args.cmd == "task":
        try:
            org, comp = parse_competition_ref(args.competition)
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        return cmd_task(cookies, bearer, org, comp, str(args.task_id))

    if args.cmd == "download-data":
        try:
            org, comp = parse_competition_ref(args.competition)
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        return cmd_download_data(
            cookies,
            bearer,
            org,
            comp,
            str(args.task_id),
            categories=args.category,
            output_dir=args.out_dir,
            output_path=args.output,
            force=args.force,
        )

    if args.cmd == "submit":
        try:
            org, comp = parse_competition_ref(args.competition)
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        return cmd_submit(
            cookies,
            bearer,
            org,
            comp,
            str(args.task_id),
            args.output,
            args.source,
            args.note,
            args.wait,
        )

    if args.cmd == "submissions":
        try:
            org, comp = parse_competition_ref(args.competition)
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        author = args.author or get_username(state)
        return cmd_submissions(
            cookies,
            bearer,
            org,
            comp,
            str(args.task_id),
            author=author,
            page=args.page,
            page_size=args.page_size,
            mode=args.mode,
        )

    if args.cmd == "submission":
        return cmd_submission(
            cookies,
            bearer,
            args.submission_id,
            org=args.org,
            comp=args.comp,
            task_id=args.task_id,
        )

    if args.cmd == "set-final":
        return cmd_set_final(cookies, bearer, args.submission_id, True)

    if args.cmd == "unset-final":
        return cmd_set_final(cookies, bearer, args.submission_id, False)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

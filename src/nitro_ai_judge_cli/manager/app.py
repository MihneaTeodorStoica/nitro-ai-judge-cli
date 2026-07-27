"""aiohttp API, dashboard, and reverse proxy for Nitro Play."""

from __future__ import annotations

import asyncio
from html import escape
from importlib import resources
import hmac
import json
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

import aiohttp
from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from ..api import hash_password, normalize_tokens
from ..play_protocol import (
    ACTION_NAMES,
    API_VERSION,
    BASE_PATH,
    ErrorType,
    MANAGER_IDENTITY,
    MANAGER_VERSION,
    MINIMUM_CLI_VERSION,
    WireError,
    competition_key,
    validate_competition,
)
from .backend import Backend, DockerBackend, LABEL_PREFIX, redact
from .store import ManagerStore


PUBLIC_API_PATHS = {
    f"{BASE_PATH}/api/v1/info",
    f"{BASE_PATH}/api/v1/health",
}
TERMINAL_OPERATION_STATES = {"complete", "failed", "cancelled", "interrupted"}
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
JUDGE_API_URL = "https://judge.nitro-ai.org/api"
REMOTE_COMPETITION_PAGE_SIZE = 200
SESSION_COOKIE = "naij_manager_session"
SESSION_SECONDS = 12 * 3600
SSE_KEEPALIVE_SECONDS = 15
SSE_QUEUE_SIZE = 1
DOCKER_EVENT_DEBOUNCE = 0.3
WORKSPACE_SORT = {
    "running": 0,
    "stopped": 1,
    "ready": 2,
    "missing": 3,
    "error": 4,
}
HEALTH_SORT = {
    "unhealthy": 0,
    "stopped": 1,
    "unknown": 2,
    "starting": 3,
    "healthy": 4,
}


def read_secret(path: str | None) -> str:
    if not path:
        return ""
    with open(path, encoding="utf-8") as stream:
        return stream.read().strip()


def json_error(error: WireError) -> web.Response:
    return web.json_response({"error": error.as_dict()}, status=error.status)


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


@web.middleware
async def errors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except WireError as exc:
        return json_error(exc)
    except web.HTTPException:
        raise
    except Exception as exc:
        request.app["logger"]("error", redact(str(exc)))
        return json_error(
            WireError(
                ErrorType.OPERATION_FAILED.value,
                "The Play manager encountered an internal error",
                logs=(redact(str(exc)),),
                status=500,
            )
        )


@web.middleware
async def security_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    app = request.app
    host = request.headers.get("Host", "").lower()
    allowed_hosts = app["allowed_hosts"]
    if not host or host not in allowed_hosts:
        raise WireError(
            ErrorType.SECURITY_ERROR.value,
            "Request Host is not allowed",
            status=400,
        )
    path = request.path
    public = path in PUBLIC_API_PATHS or path.startswith(f"{BASE_PATH}/assets/")
    dashboard_page = path in {"/", f"{BASE_PATH}", f"{BASE_PATH}/", f"{BASE_PATH}/login"}
    if not public and not dashboard_page:
        authorization = request.headers.get("Authorization", "")
        bearer = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if bearer and hmac.compare_digest(bearer, app["api_token"]):
            request["auth_kind"] = "cli"
        else:
            session_id = request.cookies.get(SESSION_COOKIE, "")
            session = _browser_session(app, session_id)
            if not session:
                direct_loopback_open = (
                    not app["lan"]
                    and request.method == "GET"
                    and path.startswith(f"{BASE_PATH}/competitions/")
                    and request.headers.get("Upgrade", "").lower() != "websocket"
                )
                if not direct_loopback_open:
                    raise WireError(
                        ErrorType.AUTHENTICATION_REQUIRED.value,
                        "Play manager authentication required",
                        status=401,
                    )
                session_id, session = _new_session(app)
                request["new_session_id"] = session_id
            request["auth_kind"] = "browser"
            request["browser_session"] = session
            browser_mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
            browser_websocket = request.headers.get("Upgrade", "").lower() == "websocket"
            if browser_mutation or browser_websocket:
                origin = request.headers.get("Origin", "")
                if origin != app["public_origin"]:
                    raise WireError(
                        ErrorType.SECURITY_ERROR.value,
                        "Request Origin is not allowed",
                        status=403,
                    )
                csrf = request.headers.get("X-CSRF-Token", "")
                manager_mutation = browser_mutation and path.startswith(
                    f"{BASE_PATH}/api/"
                )
                if manager_mutation and not hmac.compare_digest(csrf, session["csrf"]):
                    raise WireError(
                        ErrorType.SECURITY_ERROR.value,
                        "CSRF token is missing or invalid",
                        status=403,
                    )
    response = await handler(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self' ws: wss:; img-src 'self' data:; "
        "style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    new_session_id = request.get("new_session_id")
    if new_session_id:
        response.set_cookie(
            SESSION_COOKIE,
            new_session_id,
            httponly=True,
            secure=app["public_origin"].startswith("https://"),
            samesite="Strict",
            path=f"{BASE_PATH}/",
            max_age=12 * 3600,
        )
    return response


def _new_session(app: web.Application) -> tuple[str, dict[str, Any]]:
    session_id = secrets.token_urlsafe(32)
    session = {"csrf": secrets.token_urlsafe(32), "expires": time.time() + SESSION_SECONDS}
    app["sessions"][session_id] = session
    return session_id, session


def _browser_session(app: web.Application, session_id: str) -> dict[str, Any] | None:
    session = app["sessions"].get(session_id)
    if session and session["expires"] >= time.time():
        return session
    if session_id:
        app["sessions"].pop(session_id, None)
    return None


def _login_page(error: str = "", *, status: int = 200) -> web.Response:
    html = resources.files(__package__).joinpath("assets/login.html").read_text()
    return web.Response(
        text=html.replace("__LOGIN_ERROR__", escape(error)),
        status=status,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def dashboard(request: web.Request) -> web.Response:
    app = request.app
    session_id = request.cookies.get(SESSION_COOKIE, "")
    session = _browser_session(app, session_id)
    if app["lan"] and not session:
        return _login_page(
            "Your dashboard session expired. Sign in again." if session_id else ""
        )
    new_session_id = ""
    if not session:
        new_session_id, session = _new_session(app)
    html = resources.files(__package__).joinpath("assets/index.html").read_text()
    response = web.Response(
        text=(
            html.replace("__CSRF_TOKEN__", session["csrf"])
            .replace("__LAN_LOGOUT_HIDDEN__", "" if app["lan"] else "hidden")
        ),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
    if new_session_id:
        response.set_cookie(
            SESSION_COOKIE,
            new_session_id,
            httponly=True,
            secure=app["public_origin"].startswith("https://"),
            samesite="Strict",
            path=f"{BASE_PATH}/",
            max_age=SESSION_SECONDS,
        )
    return response


async def root_redirect(_: web.Request) -> web.Response:
    raise web.HTTPFound(f"{BASE_PATH}/")


async def login(request: web.Request) -> web.Response:
    app = request.app
    if not app["lan"]:
        raise web.HTTPFound(f"{BASE_PATH}/")
    peer = request.remote or "unknown"
    attempts = [value for value in app["login_attempts"].get(peer, []) if value > time.time() - 60]
    app["login_attempts"][peer] = attempts
    if len(attempts) >= 5:
        return _login_page(
            "Too many login attempts. Wait one minute and retry.", status=429
        )
    data = await request.post()
    token = str(data.get("token") or "")
    if not token or not hmac.compare_digest(token, app["dashboard_token"]):
        attempts.append(time.time())
        return _login_page("Dashboard login token is invalid.", status=401)
    session_id, _ = _new_session(app)
    response = web.HTTPFound(f"{BASE_PATH}/")
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        secure=True,
        samesite="Strict",
        path=f"{BASE_PATH}/",
        max_age=SESSION_SECONDS,
    )
    raise response


async def asset(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    content_types = {
        "app.css": "text/css",
        "app.js": "application/javascript",
        "offline.html": "text/html",
        "offline.js": "application/javascript",
        "sw.js": "application/javascript",
        "logo.svg": "image/svg+xml",
        "inter-latin.woff2": "font/woff2",
        "inter-latin-ext.woff2": "font/woff2",
        "lexend-deca-latin.woff2": "font/woff2",
        "lexend-deca-latin-ext.woff2": "font/woff2",
    }
    if name not in content_types:
        raise web.HTTPNotFound()
    content = resources.files(__package__).joinpath(f"assets/{name}").read_bytes()
    response = web.Response(body=content, content_type=content_types[name])
    if name in {"app.css", "app.js", "sw.js"}:
        response.headers["Cache-Control"] = "no-cache"
    elif name.endswith(".woff2") or name == "logo.svg":
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    if name == "sw.js":
        response.headers["Service-Worker-Allowed"] = f"{BASE_PATH}/"
    return response


async def info(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "identity": MANAGER_IDENTITY,
            "manager_version": MANAGER_VERSION,
            "api_version": API_VERSION,
            "minimum_cli_version": MINIMUM_CLI_VERSION,
            "base_path": BASE_PATH,
        }
    )


async def health(request: web.Request) -> web.Response:
    try:
        request.app["store"].competitions()
        status = "healthy"
    except Exception:
        status = "unhealthy"
    return web.json_response({"status": status}, status=200 if status == "healthy" else 503)


def _publish_refresh(app: web.Application) -> None:
    for queue in tuple(app["event_subscribers"]):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait("refresh")


def _semantic_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _semantic_snapshot(item)
            for key, item in value.items()
            if key not in {"updated_at", "created_at", "timestamp", "explicit_stopped"}
        }
    if isinstance(value, list):
        return [_semantic_snapshot(item) for item in value]
    return value


def _upsert_snapshot(
    app: web.Application,
    key: str,
    organization: str,
    competition: str,
    snapshot: dict[str, Any],
    *,
    notify: bool = True,
) -> bool:
    previous = app["store"].competition(key) or {}
    merged = dict(snapshot)
    for field in ("title", "featured", "competitionStart"):
        if field not in merged and field in previous:
            merged[field] = previous[field]
    if _semantic_snapshot(previous) == _semantic_snapshot(merged):
        return False
    app["store"].upsert_competition(key, organization, competition, merged)
    if notify:
        _publish_refresh(app)
    return True


async def _refresh_runtime(app: web.Application) -> list[dict[str, Any]]:
    backend: Any = app["backend"]
    discover = getattr(backend, "discover", None)
    if discover is not None:
        for snapshot in await discover():
            _upsert_snapshot(
                app,
                snapshot["reference"],
                snapshot["organization"],
                snapshot["competition"],
                snapshot,
            )
    return app["store"].competitions()


async def _remote_competitions(
    app: web.Application, *, allow_refresh: bool = True
) -> tuple[list[dict[str, Any]], bool]:
    credentials = app["store"].credentials()
    if not credentials:
        return [], True
    access = str(credentials.get("access_token") or "")
    base = str(credentials.get("api_base_url") or "https://judge.nitro-ai.org/api").rstrip("/")
    headers = {"Authorization": f"Bearer {access}"}
    session: ClientSession = app["http"]
    try:
        (featured, featured_status), (other, other_status) = await asyncio.gather(
            _remote_competition_pages(session, base, headers, "true"),
            _remote_competition_pages(session, base, headers, "false"),
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise WireError(
            ErrorType.OPERATION_FAILED.value,
            "Could not refresh competitions from Nitro Judge",
            stage="refreshing",
            status=502,
        ) from exc
    statuses = {featured_status, other_status}
    if 401 in statuses and allow_refresh and credentials.get("refresh_token"):
        try:
            async with session.post(
                f"{base}/auth/refreshToken",
                data={"refreshToken": credentials["refresh_token"]},
            ) as refreshed:
                if refreshed.status != 200:
                    return [], True
                tokens = await refreshed.json()
                credentials["access_token"] = (
                    tokens.get("access_token") or tokens.get("accessToken") or ""
                )
                credentials["refresh_token"] = (
                    tokens.get("refresh_token")
                    or tokens.get("refreshToken")
                    or credentials["refresh_token"]
                )
                app["store"].put_credentials(credentials)
                _publish_refresh(app)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise WireError(
                ErrorType.OPERATION_FAILED.value,
                "Could not refresh the Nitro Judge login",
                stage="refreshing",
                status=502,
            ) from exc
        return await _remote_competitions(app, allow_refresh=False)
    if statuses != {200}:
        if statuses & {401, 403}:
            return [], True
        raise WireError(
            ErrorType.OPERATION_FAILED.value,
            "Nitro Judge could not refresh the competition list",
            stage="refreshing",
            status=502,
        )
    return (
        [{**item, "featured": False} for item in other]
        + [{**item, "featured": True} for item in featured],
        False,
    )


async def _remote_competition_pages(
    session: ClientSession,
    base: str,
    headers: dict[str, str],
    featured: str,
) -> tuple[list[dict[str, Any]], int]:
    competitions: list[dict[str, Any]] = []
    previous_page: list[dict[str, Any]] | None = None
    for page in range(1, 1001):
        async with session.get(
            f"{base}/competitions",
            params={
                "page": page,
                "page_size": REMOTE_COMPETITION_PAGE_SIZE,
                "featured": featured,
            },
            headers=headers,
        ) as response:
            if response.status != 200:
                return [], response.status
            value = await response.json()
        if isinstance(value, dict):
            items = (
                value.get("competitions")
                or value.get("items")
                or value.get("data")
                or []
            )
        else:
            items = value if isinstance(value, list) else []
        current_page = [item for item in items if isinstance(item, dict)]
        if not current_page or current_page == previous_page:
            break
        competitions.extend(current_page)
        if len(current_page) < REMOTE_COMPETITION_PAGE_SIZE:
            break
        previous_page = current_page
    return competitions, 200


def _competition_sort_key(
    item: dict[str, Any],
) -> tuple[int, int, int, int, int, float, str]:
    operation = item.get("operation")
    ongoing = isinstance(operation, dict) and operation.get("status") in {
        "queued",
        "running",
    }
    image_missing = item.get("image_state") != "ready"
    started = item.get("competitionStart")
    return (
        not ongoing,
        image_missing,
        not bool(item.get("featured")) if image_missing else False,
        WORKSPACE_SORT.get(str(item.get("workspace_state")), len(WORKSPACE_SORT)),
        HEALTH_SORT.get(str(item.get("service_health")), len(HEALTH_SORT)),
        -float(started)
        if image_missing
        and isinstance(started, (int, float))
        and not isinstance(started, bool)
        else float("inf") if image_missing else 0,
        str(item.get("reference") or "").casefold(),
    )


async def competitions(request: web.Request) -> web.Response:
    cached = request.query.get("cached") == "true"
    local = (
        request.app["store"].competitions()
        if cached
        else await _refresh_runtime(request.app)
    )
    merged = {item["reference"]: item for item in local if item.get("reference")}
    sync_required = request.app["store"].credentials() is None
    if request.query.get("refresh") == "true":
        remote, sync_required = await _remote_competitions(request.app)
        for item in remote:
            org = str(item.get("organizationSlug") or item.get("organization") or "")
            competition = str(item.get("competitionSlug") or item.get("slug") or "")
            try:
                key = competition_key(org, competition)
            except WireError:
                continue
            existing = merged.get(key, {})
            snapshot = {
                **existing,
                "organization": org,
                "competition": competition,
                "reference": key,
                "title": item.get("title") or existing.get("title") or competition,
                "featured": bool(item.get("featured")),
                "competitionStart": item.get(
                    "competitionStart", existing.get("competitionStart")
                ),
                "image_state": existing.get("image_state", "missing"),
                "workspace_state": existing.get("workspace_state", "missing"),
                "service_health": existing.get("service_health", "unknown"),
            }
            merged[key] = snapshot
            _upsert_snapshot(request.app, key, org, competition, snapshot)
    for item in merged.values():
        operation = request.app["store"].latest_operation(item["reference"])
        if operation:
            item["operation"] = operation
    ordered = sorted(merged.values(), key=_competition_sort_key)
    return web.json_response(
        {
            "competitions": ordered,
            "login_sync_required": sync_required,
        }
    )


def _competition_parts(request: web.Request) -> tuple[str, str, str]:
    org, competition = validate_competition(
        request.match_info["org"], request.match_info["competition"]
    )
    return org, competition, f"{org}/{competition}"


async def competition_detail(request: web.Request) -> web.Response:
    org, competition, key = _competition_parts(request)
    snapshot = await request.app["backend"].inspect_competition(org, competition)
    _upsert_snapshot(request.app, key, org, competition, snapshot)
    return web.json_response(snapshot)


async def events(request: web.Request) -> web.StreamResponse:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=SSE_QUEUE_SIZE)
    request.app["event_subscribers"].add(queue)
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    await response.prepare(request)
    try:
        await response.write(b"retry: 2000\nevent: sync\ndata: {}\n\n")
        while True:
            try:
                event = await asyncio.wait_for(
                    queue.get(), timeout=SSE_KEEPALIVE_SECONDS
                )
                await response.write(f"event: {event}\ndata: {{}}\n\n".encode())
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        request.app["event_subscribers"].discard(queue)
    return response


async def competition_images(request: web.Request) -> web.Response:
    org, competition, _ = _competition_parts(request)
    return web.json_response(await request.app["backend"].images(org, competition))


async def competition_open(request: web.Request) -> web.Response:
    org, competition, _ = _competition_parts(request)
    snapshot = await request.app["backend"].inspect_competition(org, competition)
    if snapshot.get("workspace_state") != "running":
        raise WireError(
            ErrorType.NOT_FOUND.value,
            f"{org}/{competition} is not running",
            status=409,
        )
    base = request.app["public_url"]
    return web.json_response(
        {
            "jupyter_url": f"{base}{BASE_PATH}/competitions/{org}/{competition}/jupyter/",
            "proxy_url": f"{base}{BASE_PATH}/competitions/{org}/{competition}/proxy/",
        }
    )


async def _json_body(request: web.Request) -> dict[str, Any]:
    if request.content_length and request.content_length > 65536:
        raise WireError(ErrorType.INVALID_REQUEST.value, "Request body is too large", status=413)
    if not request.can_read_body:
        return {}
    try:
        raw = await request.read()
        if len(raw) > 65536:
            raise WireError(
                ErrorType.INVALID_REQUEST.value,
                "Request body is too large",
                status=413,
            )
        value = json.loads(raw or b"{}")
    except (ValueError, json.JSONDecodeError):
        raise WireError(ErrorType.INVALID_REQUEST.value, "Request body must be JSON", status=400)
    if not isinstance(value, dict):
        raise WireError(ErrorType.INVALID_REQUEST.value, "Request body must be an object", status=400)
    return value


async def competition_action(request: web.Request) -> web.Response:
    org, competition, key = _competition_parts(request)
    action = request.match_info["action"]
    if action not in ACTION_NAMES:
        raise WireError(ErrorType.INVALID_REQUEST.value, f"Unknown Play action: {action}", status=404)
    options = await _json_body(request)
    if action == "delete-workspace":
        force = options.get("force") is True
        if not force and options.get("confirm_ref") != key:
            raise WireError(
                ErrorType.INVALID_REQUEST.value,
                f"Deleting a workspace requires confirm_ref={key!r} or force=true",
                stage="validating",
                status=409,
            )
    async with request.app["operation_guard"]:
        active = request.app["store"].active_operation(key)
        if active:
            if active["action"] == action and active["options"] == options:
                return web.json_response(
                    {"operation_id": active["id"], "operation": active}, status=202
                )
            raise WireError(
                ErrorType.COMPETITION_BUSY.value,
                f"{key} is busy with {active['action']}",
                stage=active["stage"],
                status=409,
            )
        operation_id = str(uuid.uuid4())
        if request.app["store"].competition(key) is None:
            _upsert_snapshot(
                request.app,
                key,
                org,
                competition,
                {
                    "organization": org,
                    "competition": competition,
                    "reference": key,
                    "title": competition,
                    "image_state": "missing",
                    "workspace_state": "missing",
                    "service_health": "unknown",
                },
            )
        operation = request.app["store"].create_operation(
            operation_id, key, action, options
        )
        _track_operation_task(
            request.app,
            operation_id,
            _run_operation(request.app, operation_id, org, competition, action, options),
        )
        _publish_refresh(request.app)
    return web.json_response(
        {"operation_id": operation_id, "operation": operation}, status=202
    )


def _mark_operation_cancelled(
    app: web.Application, operation_id: str
) -> dict[str, Any] | None:
    value = app["store"].operation(operation_id)
    if value is not None and value["status"] not in TERMINAL_OPERATION_STATES:
        app["store"].fail(
            operation_id,
            {
                "type": ErrorType.OPERATION_FAILED.value,
                "message": "Operation cancelled",
                "stage": "cancelled",
                "logs": [],
            },
            status="cancelled",
        )
        _publish_refresh(app)
    return app["store"].operation(operation_id)


def _track_operation_task(
    app: web.Application, operation_id: str, coroutine: Any
) -> asyncio.Task[None]:
    task = asyncio.create_task(coroutine)
    app["operation_tasks"][operation_id] = task

    def done(completed: asyncio.Task[None]) -> None:
        if app["operation_tasks"].get(operation_id) is completed:
            app["operation_tasks"].pop(operation_id, None)
        if completed.cancelled():
            _mark_operation_cancelled(app, operation_id)

    task.add_done_callback(done)
    return task


async def _run_operation(
    app: web.Application,
    operation_id: str,
    org: str,
    competition: str,
    action: str,
    options: dict[str, Any],
) -> None:
    key = f"{org}/{competition}"

    async def progress(stage: str, message: str) -> None:
        app["store"].event(operation_id, stage, redact(message))

    try:
        await progress("validating", "Validating competition and Docker state")
        result = await app["backend"].perform(
            org,
            competition,
            action,
            options,
            progress,
            app["store"].adoption(key),
        )
        _upsert_snapshot(app, key, org, competition, result)
        if action == "delete-workspace":
            app["store"].delete_adoption(key)
        if action == "stop":
            app["store"].set_explicit_stopped(key, True)
        elif action in {"play", "start", "restart", "recreate"}:
            app["store"].set_explicit_stopped(key, False)
        app["store"].finish(operation_id, result)
    except asyncio.CancelledError:
        _mark_operation_cancelled(app, operation_id)
    except WireError as exc:
        app["store"].fail(operation_id, exc.as_dict())
    except Exception as exc:
        app["store"].fail(
            operation_id,
            {
                "type": ErrorType.OPERATION_FAILED.value,
                "message": redact(str(exc)),
                "stage": "failed",
                "logs": [],
            },
        )
    finally:
        app["operation_tasks"].pop(operation_id, None)
        _publish_refresh(app)


async def operation_detail(request: web.Request) -> web.Response:
    value = request.app["store"].operation(request.match_info["operation_id"])
    if value is None:
        raise WireError(ErrorType.NOT_FOUND.value, "Operation not found", status=404)
    return web.json_response(value)


async def operation_cancel(request: web.Request) -> web.Response:
    operation_id = request.match_info["operation_id"]
    value = request.app["store"].operation(operation_id)
    if value is None:
        raise WireError(ErrorType.NOT_FOUND.value, "Operation not found", status=404)
    if value["status"] in TERMINAL_OPERATION_STATES:
        return web.json_response(value)
    task = request.app["operation_tasks"].get(operation_id)
    if task:
        if not task.done():
            task.cancel()
        await asyncio.gather(asyncio.shield(task), return_exceptions=True)
        if request.app["operation_tasks"].get(operation_id) is task:
            request.app["operation_tasks"].pop(operation_id, None)
    return web.json_response(_mark_operation_cancelled(request.app, operation_id))


async def logs(request: web.Request) -> web.Response:
    org, competition, _ = _competition_parts(request)
    try:
        tail = max(1, min(int(request.query.get("tail", "80")), 2000))
    except ValueError:
        raise WireError(ErrorType.INVALID_REQUEST.value, "tail must be an integer", status=400)
    value = await request.app["backend"].logs(org, competition, tail)
    return web.json_response({"logs": redact(value), "tail": tail})


async def logs_follow(request: web.Request) -> web.StreamResponse:
    org, competition, _ = _competition_parts(request)
    response = web.StreamResponse(
        status=200, headers={"Content-Type": "application/x-ndjson", "Cache-Control": "no-store"}
    )
    await response.prepare(request)
    previous: list[str] = []
    try:
        while True:
            current = redact(await request.app["backend"].logs(org, competition, 200)).splitlines()
            overlap = min(len(previous), len(current))
            while overlap and previous[-overlap:] != current[:overlap]:
                overlap -= 1
            for line in current[overlap:]:
                await response.write((json.dumps({"line": line}) + "\n").encode())
            previous = current
            await asyncio.sleep(1)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


async def put_credentials(request: web.Request) -> web.Response:
    value = await _json_body(request)
    allowed = {
        "access_token",
        "refresh_token",
        "access_token_exp",
        "refresh_token_exp",
        "username",
        "api_base_url",
    }
    normalized = {key: value.get(key) for key in allowed if key in value}
    if not normalized.get("access_token") or not normalized.get("refresh_token"):
        raise WireError(
            ErrorType.INVALID_REQUEST.value,
            "Both normalized access and refresh tokens are required",
            status=400,
        )
    request.app["store"].put_credentials(normalized)
    _publish_refresh(request.app)
    return web.json_response({"synchronized": True})


async def delete_credentials(request: web.Request) -> web.Response:
    request.app["store"].delete_credentials()
    _publish_refresh(request.app)
    return web.json_response({"synchronized": False})


async def logout(request: web.Request) -> web.Response:
    if not request.app["lan"] or request.get("auth_kind") != "browser":
        raise WireError(
            ErrorType.SECURITY_ERROR.value,
            "Browser logout is available only for LAN dashboard sessions",
            status=403,
        )
    session_id = request.cookies.get(SESSION_COOKIE, "")
    request.app["sessions"].pop(session_id, None)
    response = web.json_response({"logged_out": True})
    response.del_cookie(SESSION_COOKIE, path=f"{BASE_PATH}/")
    _publish_refresh(request.app)
    return response


async def nitro_login(request: web.Request) -> web.Response:
    value = await _json_body(request)
    username = str(value.get("username") or "").strip()
    password = str(value.get("password") or "")
    if not username or not password or len(username) > 256 or len(password) > 4096:
        raise WireError(
            ErrorType.INVALID_REQUEST.value,
            "Username and password are required",
            status=400,
        )
    try:
        async with request.app["http"].post(
            f"{request.app['judge_api_url']}/auth/login",
            data={"username": username, "password": hash_password(password)},
        ) as response:
            try:
                tokens = await response.json(content_type=None)
            except (ValueError, json.JSONDecodeError):
                tokens = {}
            response_username = response.headers.get("x-set-username", username)
            status = response.status
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise WireError(
            ErrorType.OPERATION_FAILED.value,
            "Nitro login is temporarily unavailable",
            status=502,
        ) from exc
    if status != 200 or not isinstance(tokens, dict):
        message = tokens.get("message") or tokens.get("error") if isinstance(tokens, dict) else None
        raise WireError(
            ErrorType.AUTHENTICATION_REQUIRED.value,
            str(message or "Nitro login failed"),
            status=401,
        )
    credentials = normalize_tokens(tokens, response_username)
    if not credentials["access_token"] or not credentials["refresh_token"]:
        raise WireError(
            ErrorType.AUTHENTICATION_REQUIRED.value,
            "Nitro login response did not contain usable credentials",
            status=502,
        )
    credentials["api_base_url"] = request.app["judge_api_url"]
    request.app["store"].put_credentials(credentials)
    _publish_refresh(request.app)
    return web.json_response({"authenticated": True, "username": credentials["username"]})


async def legacy_adoptions(request: web.Request) -> web.Response:
    value = await _json_body(request)
    manifests = value.get("manifests")
    if not isinstance(manifests, list) or len(manifests) > 500:
        raise WireError(
            ErrorType.INVALID_REQUEST.value,
            "manifests must be a bounded list",
            status=400,
        )
    adopted = 0
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        org = str(manifest.get("organization") or "")
        competition = str(manifest.get("competition") or "")
        try:
            key = competition_key(org, competition)
        except WireError:
            continue
        sanitized = {
            field: manifest.get(field)
            for field in (
                "reference",
                "organization",
                "competition",
                "project",
                "container_id",
                "running",
                "workspace_kind",
                "workspace_volume",
                "notebook_image",
                "proxy_image",
            )
        }
        request.app["store"].put_adoption(
            key, sanitized, bool(manifest.get("verified"))
        )
        adopted += 1
    if adopted:
        _publish_refresh(request.app)
    return web.json_response({"adopted": adopted})


def _forward_headers(request: web.Request, target_host: str) -> dict[str, str]:
    headers = {}
    for key, value in request.headers.items():
        lowered = key.lower()
        if lowered in HOP_HEADERS or lowered == "host":
            continue
        if lowered == "authorization":
            bearer = value.removeprefix("Bearer ") if value.startswith("Bearer ") else ""
            if bearer and hmac.compare_digest(bearer, request.app["api_token"]):
                continue
        if lowered == "x-csrf-token":
            continue
        if lowered == "cookie":
            cookies = [
                part.strip()
                for part in value.split(";")
                if part.strip().partition("=")[0] != SESSION_COOKIE
            ]
            if not cookies:
                continue
            value = "; ".join(cookies)
        headers[key] = value
    headers["Host"] = target_host
    headers["X-Forwarded-Proto"] = request.app["public_origin"].split(":", 1)[0]
    headers["X-Forwarded-Host"] = request.headers.get("Host", "")
    headers["X-Forwarded-Prefix"] = BASE_PATH
    if request.remote:
        headers["X-Forwarded-For"] = request.remote
    return headers


async def _proxy_websocket(request: web.Request, target: str, headers: dict[str, str]) -> web.WebSocketResponse:
    downstream = web.WebSocketResponse(autoping=True)
    await downstream.prepare(request)
    session: ClientSession = request.app["http"]
    async with session.ws_connect(target, headers=headers, autoping=True) as upstream:
        async def client_to_upstream() -> None:
            async for message in downstream:
                if message.type == WSMsgType.TEXT:
                    await upstream.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await upstream.send_bytes(message.data)
                elif message.type == WSMsgType.CLOSE:
                    await upstream.close()

        async def upstream_to_client() -> None:
            async for message in upstream:
                if message.type == WSMsgType.TEXT:
                    await downstream.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await downstream.send_bytes(message.data)
                elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}:
                    await downstream.close()

        tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
    return downstream


async def reverse_proxy(request: web.Request) -> web.StreamResponse:
    org, competition, _ = _competition_parts(request)
    role = request.match_info["role"]
    tail = request.match_info.get("tail", "")
    names = request.app["backend"].names(org, competition)
    if role == "jupyter":
        target_host = f"{names['jupyter_alias']}:8888"
        target_path = request.path_qs
    elif role == "proxy":
        target_host = f"{names['proxy_alias']}:9000"
        target_path = "/" + tail
        if request.query_string:
            target_path += "?" + request.query_string
    else:
        raise web.HTTPNotFound()
    target = f"http://{target_host}{target_path}"
    headers = _forward_headers(request, target_host)
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _proxy_websocket(request, target, headers)
    session: ClientSession = request.app["http"]
    async with session.request(
        request.method,
        target,
        headers=headers,
        data=request.content if request.can_read_body else None,
        allow_redirects=False,
    ) as upstream:
        stable_prefix = f"{BASE_PATH}/competitions/{org}/{competition}/{role}/"
        response_headers: list[tuple[str, str]] = []
        for key, value in upstream.headers.items():
            lowered = key.lower()
            if lowered in HOP_HEADERS or lowered == "content-length":
                continue
            if lowered == "location":
                internal_origin = f"http://{target_host}"
                if value.startswith(internal_origin):
                    value = value[len(internal_origin) :]
                relative_prefix = stable_prefix.lstrip("/").rstrip("/")
                if value == relative_prefix or value.startswith(relative_prefix + "?"):
                    value = stable_prefix + value[len(relative_prefix) :]
                elif value.startswith(relative_prefix + "/"):
                    value = "/" + value
                elif value.startswith("/") and not value.startswith(stable_prefix):
                    value = stable_prefix + value.lstrip("/")
            elif lowered == "set-cookie":
                value = re.sub(r"(?i);\s*Domain=[^;]+", "", value)
                if role == "proxy":
                    if re.search(r"(?i);\s*Path=/($|;)", value):
                        value = re.sub(
                            r"(?i)(;\s*Path=)/(?=$|;)",
                            rf"\1{stable_prefix}",
                            value,
                        )
                    elif "path=" not in value.lower():
                        value += f"; Path={stable_prefix}"
            response_headers.append((key, value))
        response = web.StreamResponse(status=upstream.status, headers=response_headers)
        script_src = "'self' 'unsafe-inline' 'unsafe-eval'" if role == "jupyter" else "'self' 'unsafe-inline'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self' ws: wss:; img-src 'self' data:; "
            f"style-src 'self' 'unsafe-inline'; script-src {script_src}; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
        await response.write_eof()
        return response


def _docker_event_time(event: dict[str, Any]) -> float | None:
    value = event.get("timeNano")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1_000_000_000
    value = event.get("time")
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _docker_event_competitions(
    app: web.Application, event: dict[str, Any]
) -> set[str]:
    actor = event.get("Actor")
    attributes = actor.get("Attributes") if isinstance(actor, dict) else None
    attributes = attributes if isinstance(attributes, dict) else {}
    identity = str(attributes.get(f"{LABEL_PREFIX}.identity") or "")
    owner = str(attributes.get(f"{LABEL_PREFIX}.owner") or "")
    if identity and owner == MANAGER_IDENTITY:
        try:
            org, competition = identity.split("/", 1)
            return {competition_key(org, competition)}
        except (ValueError, WireError):
            return set()
    if str(event.get("Type") or "").lower() != "image":
        return set()
    candidates = {
        str(value)
        for value in (
            attributes.get("name"),
            attributes.get("image"),
            event.get("from"),
            actor.get("ID") if isinstance(actor, dict) else None,
        )
        if value
    }
    matches = set()
    backend = app["backend"]
    for snapshot in app["store"].competitions():
        reference = str(snapshot.get("reference") or "")
        if "/" not in reference:
            continue
        org, competition = reference.split("/", 1)
        expected = set(backend.image_names(org, competition))
        expected.update(backend.fallback_aliases(org, competition))
        for image in (snapshot.get("images") or {}).values():
            if isinstance(image, dict) and image.get("name"):
                expected.add(str(image["name"]))
        if candidates & expected:
            matches.add(reference)
    return matches


def _queue_docker_reconcile(app: web.Application, reference: str) -> None:
    previous = app["docker_debounce_tasks"].get(reference)
    if previous:
        previous.cancel()

    async def reconcile() -> None:
        await asyncio.sleep(DOCKER_EVENT_DEBOUNCE)
        org, competition = reference.split("/", 1)
        snapshot = await app["backend"].inspect_competition(org, competition)
        _upsert_snapshot(app, reference, org, competition, snapshot)

    task = asyncio.create_task(reconcile())
    app["docker_debounce_tasks"][reference] = task

    def done(completed: asyncio.Task[None]) -> None:
        if app["docker_debounce_tasks"].get(reference) is completed:
            app["docker_debounce_tasks"].pop(reference, None)
        if not completed.cancelled() and (error := completed.exception()) is not None:
            app["logger"]("error", redact(str(error)))

    task.add_done_callback(done)


async def _watch_docker_events(app: web.Application) -> None:
    backoff = 1

    async def received(event: dict[str, Any]) -> None:
        timestamp = _docker_event_time(event)
        if timestamp is not None:
            app["docker_event_since"] = timestamp
        for reference in _docker_event_competitions(app, event):
            _queue_docker_reconcile(app, reference)

    while True:
        try:
            await app["backend"].watch_events(
                received, since=app["docker_event_since"]
            )
            backoff = 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app["logger"]("error", f"Docker event watcher: {redact(str(exc))}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


async def on_startup(app: web.Application) -> None:
    app["http"] = ClientSession(
        timeout=ClientTimeout(total=30), cookie_jar=aiohttp.DummyCookieJar()
    )
    await _refresh_runtime(app)
    if callable(getattr(app["backend"], "watch_events", None)):
        app["docker_watcher"] = asyncio.create_task(_watch_docker_events(app))


async def on_cleanup(app: web.Application) -> None:
    watcher = app.get("docker_watcher")
    if watcher:
        watcher.cancel()
    for task in list(app["docker_debounce_tasks"].values()):
        task.cancel()
    for task in list(app["operation_tasks"].values()):
        task.cancel()
    if watcher:
        await asyncio.gather(watcher, return_exceptions=True)
    await asyncio.gather(*app["docker_debounce_tasks"].values(), return_exceptions=True)
    await asyncio.gather(*app["operation_tasks"].values(), return_exceptions=True)
    await app["http"].close()
    app["store"].close()


def create_app(
    *,
    backend: Backend | None = None,
    store: ManagerStore | None = None,
    api_token: str | None = None,
    dashboard_token: str | None = None,
    public_url: str | None = None,
    lan: bool | None = None,
) -> web.Application:
    state_path = os.environ.get("NAIJ_MANAGER_STATE", "/var/lib/naij/manager.db")
    projects = os.environ.get("NAIJ_MANAGER_PROJECTS", "/var/lib/naij/projects")
    manager_image = os.environ.get("NAIJ_MANAGER_IMAGE", "naij-play-manager:dev")
    api_token = api_token if api_token is not None else read_secret(os.environ.get("NAIJ_MANAGER_API_TOKEN_FILE"))
    if not api_token:
        raise RuntimeError("NAIJ_MANAGER_API_TOKEN_FILE is missing or empty")
    dashboard_token = (
        dashboard_token
        if dashboard_token is not None
        else read_secret(os.environ.get("NAIJ_MANAGER_DASHBOARD_TOKEN_FILE"))
    )
    public_url = (public_url or os.environ.get("NAIJ_MANAGER_PUBLIC_URL") or "http://localhost:51123").rstrip("/")
    lan = bool(int(os.environ.get("NAIJ_MANAGER_LAN", "0"))) if lan is None else lan
    app = web.Application(
        middlewares=[errors_middleware, security_middleware], client_max_size=2 * 1024**3
    )
    app["store"] = store or ManagerStore(state_path)
    app["backend"] = backend or DockerBackend(projects, manager_image)
    app["api_token"] = api_token
    app["dashboard_token"] = dashboard_token or ""
    app["public_url"] = public_url
    app["public_origin"] = _origin(public_url)
    parsed = urlsplit(public_url)
    hosts = {parsed.netloc.lower()}
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not lan:
        hosts.update({f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"})
    app["allowed_hosts"] = hosts
    app["lan"] = lan
    app["sessions"] = {}
    app["login_attempts"] = {}
    app["operation_guard"] = asyncio.Lock()
    app["operation_tasks"] = {}
    app["event_subscribers"] = set()
    app["docker_debounce_tasks"] = {}
    app["docker_event_since"] = time.time()
    app["logger"] = lambda level, message: print(f"{level}: {message}", flush=True)
    app["judge_api_url"] = JUDGE_API_URL

    router = app.router
    router.add_get("/", root_redirect)
    router.add_get(f"{BASE_PATH}", dashboard)
    router.add_get(f"{BASE_PATH}/", dashboard)
    router.add_post(f"{BASE_PATH}/login", login)
    router.add_get(f"{BASE_PATH}/assets/{{name}}", asset)
    router.add_get(f"{BASE_PATH}/api/v1/info", info)
    router.add_get(f"{BASE_PATH}/api/v1/health", health)
    router.add_get(f"{BASE_PATH}/api/v1/competitions", competitions)
    router.add_get(f"{BASE_PATH}/api/v1/events", events)
    router.add_get(f"{BASE_PATH}/api/v1/competitions/{{org}}/{{competition}}", competition_detail)
    router.add_get(f"{BASE_PATH}/api/v1/competitions/{{org}}/{{competition}}/images", competition_images)
    router.add_get(f"{BASE_PATH}/api/v1/competitions/{{org}}/{{competition}}/open", competition_open)
    router.add_post(
        f"{BASE_PATH}/api/v1/competitions/{{org}}/{{competition}}/actions/{{action}}",
        competition_action,
    )
    router.add_get(f"{BASE_PATH}/api/v1/competitions/{{org}}/{{competition}}/logs", logs)
    router.add_get(
        f"{BASE_PATH}/api/v1/competitions/{{org}}/{{competition}}/logs/follow",
        logs_follow,
    )
    router.add_get(f"{BASE_PATH}/api/v1/operations/{{operation_id}}", operation_detail)
    router.add_post(f"{BASE_PATH}/api/v1/operations/{{operation_id}}/cancel", operation_cancel)
    router.add_put(f"{BASE_PATH}/api/v1/credentials", put_credentials)
    router.add_delete(f"{BASE_PATH}/api/v1/credentials", delete_credentials)
    router.add_post(f"{BASE_PATH}/api/v1/logout", logout)
    router.add_post(f"{BASE_PATH}/api/v1/login", nitro_login)
    router.add_post(f"{BASE_PATH}/api/v1/legacy-adoptions", legacy_adoptions)
    router.add_route(
        "*",
        f"{BASE_PATH}/competitions/{{org}}/{{competition}}/{{role:jupyter|proxy}}/{{tail:.*}}",
        reverse_proxy,
    )
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

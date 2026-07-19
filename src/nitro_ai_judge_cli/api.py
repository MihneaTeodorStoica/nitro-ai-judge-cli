"""HTTP transport, authentication, refresh, and payload parsing."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import getpass
import hashlib
import json
import time
import urllib.error as urllib_error
import urllib.parse
import urllib.request as urllib_request
import uuid
from typing import Any

from . import config
from .config import BASE_URL, USER_AGENT
from .state import CredentialsError, load_state, save_state

UA = USER_AGENT


def configure_runtime(api_url: str | None, submission_proxy: bool) -> None:
    config.configure_runtime(api_url, submission_proxy)


@dataclass(frozen=True)
class APIClient:
    """A small transport object for site and configured API requests."""

    api_base_url: str
    cookies: tuple[str, str] | None = None
    bearer: str = ""

    @classmethod
    def from_runtime(
        cls,
        *,
        cookies: tuple[str, str] | None = None,
        bearer: str = "",
    ) -> "APIClient":
        return cls(config.runtime().api_base_url, cookies, bearer)

    @classmethod
    def authenticated(cls, state: dict[str, Any]) -> "APIClient":
        auth = get_auth(state)
        if not auth:
            raise ValueError("state does not contain a usable access token")
        return cls.from_runtime(
            cookies=(auth[0] or "", auth[1] or ""), bearer=auth[2]
        )

    def api_request(self, path: str, **kwargs: Any) -> tuple[int, bytes, dict[str, str]]:
        return request(
            path,
            cookies=self.cookies,
            bearer=self.bearer,
            base_url=self.api_base_url,
            **kwargs,
        )

    def api_text(self, path: str, **kwargs: Any) -> tuple[int, str, dict[str, str]]:
        status, body, headers = self.api_request(path, **kwargs)
        return status, body.decode("utf-8", errors="replace"), headers

    def site_request(self, path: str, **kwargs: Any) -> tuple[int, bytes, dict[str, str]]:
        return request(
            path,
            cookies=self.cookies,
            bearer=self.bearer,
            base_url=BASE_URL,
            **kwargs,
        )

    def site_text(self, path: str, **kwargs: Any) -> tuple[int, str, dict[str, str]]:
        status, body, headers = self.site_request(path, **kwargs)
        return status, body.decode("utf-8", errors="replace"), headers

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
    return request(base_url=config.runtime().api_base_url, **kwargs)

def request_text(**kwargs: Any) -> tuple[int, str, dict[str, str]]:
    status, body, headers = request(**kwargs)
    return status, body.decode("utf-8", errors="replace"), headers

def api_request_text(**kwargs: Any) -> tuple[int, str, dict[str, str]]:
    return request_text(base_url=config.runtime().api_base_url, **kwargs)

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
    boundary = f"----NAIJ{uuid.uuid4().hex}"
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
    state = normalize_tokens(tokens, username)
    state["timestamp"] = time.time()
    save_state(state)

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
        print(f"Login OK | user={state.get('username')} | role={state.get('role')}")
        return 0

    print(f"Login failed: {result.get('error')}")
    return 1


def require_auth() -> tuple[dict[str, Any], tuple[str, str], str] | None:
    try:
        state = load_state()
    except CredentialsError as exc:
        print(f"Error: {exc}")
        return None
    if not state:
        print("Not logged in. Run: naij login")
        return None
    state = ensure_fresh_state(state)
    if not state:
        print("Saved login expired. Run: naij login")
        return None
    auth = get_auth(state)
    if not auth:
        print("Missing access token. Run: naij login")
        return None
    return state, (auth[0] or "", auth[1] or ""), auth[2]

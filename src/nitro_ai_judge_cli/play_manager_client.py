"""Authenticated stdlib client for the Dockerized Play manager."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Iterator
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .play_protocol import BASE_PATH, ErrorType, WireError
from .state import resolve_state_paths


class ManagerConnectionError(RuntimeError):
    """The configured manager could not be reached."""


class ManagerClient:
    def __init__(self, base_url: str, token: str = "", *, timeout: float = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout

    @classmethod
    def from_state(cls) -> "ManagerClient":
        root = resolve_state_paths().play_manager
        config_path = os.path.join(root, "manager.json")
        token_path = os.path.join(root, "cli-api-token")
        try:
            with open(config_path, encoding="utf-8") as stream:
                config = json.load(stream)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise ManagerConnectionError(
                "Play manager is not installed; run `naij play manager install`"
            ) from exc
        try:
            with open(token_path, encoding="utf-8") as stream:
                token = stream.read().strip()
        except OSError as exc:
            raise ManagerConnectionError(
                "Play manager credential is missing; run `naij play manager update`"
            ) from exc
        public_url = str(config.get("public_url") or "").rstrip("/")
        if not public_url:
            scheme = "https" if config.get("tls_cert") else "http"
            bind = str(config.get("bind") or "127.0.0.1")
            host = "localhost" if bind in {"127.0.0.1", "::1"} else bind
            public_url = f"{scheme}://{host}:{int(config.get('port') or 51123)}"
        return cls(public_url, token)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "User-Agent": f"NAIJ/{__version__}",
        }
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if authenticated:
            if not self.token:
                raise ManagerConnectionError("Play manager API credential is missing")
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except (OSError, urllib.error.URLError) as exc:
            raise ManagerConnectionError(
                f"Play manager is unavailable at {self.base_url}{BASE_PATH}/"
            ) from exc
        try:
            parsed = json.loads(raw or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManagerConnectionError(
                f"Unexpected response from Play manager (HTTP {status})"
            ) from exc
        if not isinstance(parsed, dict):
            raise ManagerConnectionError("Unexpected non-object Play manager response")
        if status >= 400:
            error = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
            raise WireError(
                str(error.get("type") or ErrorType.OPERATION_FAILED.value),
                str(error.get("message") or f"Play manager returned HTTP {status}"),
                str(error.get("stage")) if error.get("stage") else None,
                tuple(str(item) for item in error.get("logs", [])[-40:]),
                status,
            )
        return parsed

    def info(self) -> dict[str, Any]:
        return self._request("GET", f"{BASE_PATH}/api/v1/info", authenticated=False)

    def health(self) -> dict[str, Any]:
        return self._request("GET", f"{BASE_PATH}/api/v1/health", authenticated=False)

    def competitions(self) -> list[dict[str, Any]]:
        value = self._request("GET", f"{BASE_PATH}/api/v1/competitions")
        return [item for item in value.get("competitions", []) if isinstance(item, dict)]

    def competition(self, org: str, competition: str) -> dict[str, Any]:
        return self._request(
            "GET", f"{BASE_PATH}/api/v1/competitions/{org}/{competition}"
        )

    def images(self, org: str, competition: str) -> dict[str, Any]:
        return self._request(
            "GET", f"{BASE_PATH}/api/v1/competitions/{org}/{competition}/images"
        )

    def open_info(self, org: str, competition: str) -> dict[str, Any]:
        return self._request(
            "GET", f"{BASE_PATH}/api/v1/competitions/{org}/{competition}/open"
        )

    def action(
        self,
        org: str,
        competition: str,
        action: str,
        **options: Any,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"{BASE_PATH}/api/v1/competitions/{org}/{competition}/actions/{action}",
            {key: value for key, value in options.items() if value is not None},
        )

    def operation(self, operation_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"{BASE_PATH}/api/v1/operations/{urllib.parse.quote(operation_id)}"
        )

    def cancel(self, operation_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"{BASE_PATH}/api/v1/operations/{urllib.parse.quote(operation_id)}/cancel",
            {},
        )

    def wait_operation(
        self,
        operation_id: str,
        *,
        timeout: float | None = 600,
        interval: float = 0.5,
        progress: Callable[[dict[str, Any]], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        deadline = None if timeout is None else time.monotonic() + timeout
        last_sequence = -1
        while deadline is None or time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("Stopped waiting for Play operation")
            operation = self.operation(operation_id)
            events = operation.get("events", [])
            for event in events if isinstance(events, list) else []:
                sequence = int(event.get("sequence", -1)) if isinstance(event, dict) else -1
                if sequence > last_sequence and progress is not None:
                    progress(event)
                last_sequence = max(last_sequence, sequence)
            status = str(operation.get("status") or "")
            if status == "complete":
                return operation
            if status in {"failed", "cancelled", "interrupted"}:
                error = operation.get("error") or {}
                raise WireError(
                    str(error.get("type") or ErrorType.OPERATION_FAILED.value),
                    str(error.get("message") or f"Operation {status}"),
                    str(error.get("stage") or operation.get("stage") or "") or None,
                    tuple(str(item) for item in error.get("logs", [])[-40:]),
                    500,
                )
            if stop_event is not None:
                if stop_event.wait(interval):
                    raise InterruptedError("Stopped waiting for Play operation")
            else:
                time.sleep(interval)
        assert timeout is not None
        raise ManagerConnectionError(
            f"Play operation did not finish within {int(timeout)} seconds"
        )

    def logs(self, org: str, competition: str, *, tail: int = 80) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{BASE_PATH}/api/v1/competitions/{org}/{competition}/logs?tail={tail}",
        )

    def follow_logs(self, org: str, competition: str) -> Iterator[str]:
        headers = {
            "Accept": "application/x-ndjson",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": f"NAIJ/{__version__}",
        }
        request = urllib.request.Request(
            f"{self.base_url}{BASE_PATH}/api/v1/competitions/{org}/{competition}/logs/follow",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=None) as response:
                for raw in response:
                    try:
                        event = json.loads(raw)
                        line = event["line"]
                        if not isinstance(line, str):
                            raise TypeError
                    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
                        raise ManagerConnectionError(
                            "Unexpected response from Play manager log stream"
                        ) from exc
                    yield line + "\n"
        except urllib.error.HTTPError as exc:
            try:
                parsed = json.loads(exc.read(65536) or b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError) as parse_error:
                raise ManagerConnectionError(
                    f"Unexpected response from Play manager (HTTP {exc.code})"
                ) from parse_error
            error = parsed.get("error") if isinstance(parsed, dict) else None
            error = error if isinstance(error, dict) else {}
            raise WireError(
                str(error.get("type") or ErrorType.OPERATION_FAILED.value),
                str(error.get("message") or f"Play manager returned HTTP {exc.code}"),
                str(error.get("stage")) if error.get("stage") else None,
                tuple(str(item) for item in error.get("logs", [])[-40:]),
                exc.code,
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ManagerConnectionError("Play log stream disconnected") from exc

    def put_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"{BASE_PATH}/api/v1/credentials", credentials)

    def delete_credentials(self) -> dict[str, Any]:
        return self._request("DELETE", f"{BASE_PATH}/api/v1/credentials")

    def adopt_legacy(self, manifests: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            "POST", f"{BASE_PATH}/api/v1/legacy-adoptions", {"manifests": manifests}
        )

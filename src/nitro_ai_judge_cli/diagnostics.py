"""Read-only diagnostics; never loads state through the migrating/hardening path."""
from __future__ import annotations

import json
import time
import base64
import platform
import sys
import urllib.error
import urllib.request
import os
from pathlib import Path
import shutil
import stat
import subprocess
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .config import runtime
from .state import inspect_state_paths


def public_url(value: str) -> str:
    """Omit credentials, query strings and fragments from diagnostic URLs."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        return urlunsplit((parsed.scheme, host + (f":{port}" if port else ""), parsed.path, "", ""))
    except ValueError:
        return "(invalid URL)"


def _json_state(path: Path) -> tuple[str, dict]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return "invalid", {}
        value = json.loads(raw)
        return ("present", value) if isinstance(value, dict) else ("invalid", {})
    except FileNotFoundError:
        return "missing", {}
    except (OSError, ValueError):
        return "invalid", {}


def api_reachability(url: str) -> dict:
    from .api import _open_once
    try:
        request = urllib.request.Request(public_url(url), method="HEAD")
        with _open_once(request, 2) as response:
            return {"reachable": True, "http_status": response.status, "authenticated": "not tested"}
    except urllib.error.HTTPError as exc:
        exc.close()
        return {"reachable": True, "http_status": exc.code, "authenticated": "not tested"}
    except Exception:
        return {"reachable": False, "authenticated": "not tested"}


def docker_context() -> str:
    executable = shutil.which("docker")
    if not executable:
        return "missing"
    try:
        result = subprocess.run([executable, "context", "show"], capture_output=True, timeout=2, check=False, text=True)
        name = result.stdout.strip()
        return name if name in {"default", "desktop-linux", "rootless"} else "custom (name withheld)" if result.returncode == 0 else "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def collect_diagnostics() -> dict:
    paths = inspect_state_paths()
    credentials, tokens = _json_state(Path(paths.credentials))
    manager, configuration = _json_state(Path(paths.play_manager) / "manager.json")
    freshness = {}
    for kind in ("access", "refresh"):
        token = tokens.get(kind + "_token")
        expires = tokens.get(kind + "_token_exp")
        if not isinstance(expires, (int, float)) and isinstance(token, str):
            try:
                part = token.split(".")[1]
                expires = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("exp")
            except (ValueError, IndexError, TypeError, AttributeError):
                expires = None
        freshness[kind] = ("missing" if not token else "expired" if isinstance(expires, (int, float)) and expires <= time.time()
                           else "unexpired" if isinstance(expires, (int, float)) else "unknown")
    manager_live = {"status": "not installed"}
    if manager == "present":
        from .play_manager_client import ManagerClient
        from .play_manager_lifecycle import verify_manager_info
        url = configuration.get("public_url")
        if not url:
            host = str(configuration.get("bind") or "127.0.0.1")
            if ":" in host:
                host = f"[{host}]"
            url = f"{'https' if configuration.get('tls_cert') else 'http'}://{host}:{configuration.get('port') or 51123}"
        manager_live["url"] = public_url(str(url))
        try:
            client = ManagerClient(str(url), timeout=2)
            info = client.info()
            verify_manager_info(info)
            manager_live.update(status=client.health().get("status", "unknown"),
                                identity=info.get("identity"), version=info.get("manager_version"), api_version=info.get("api_version"))
        except Exception:
            manager_live["status"] = "unavailable or incompatible; inspect naij play manager status"
    try:
        permissions = oct(stat.S_IMODE(os.stat(paths.root).st_mode))
    except OSError:
        permissions = None
    tools = {}
    for name in ("podman", "docker"):
        executable = shutil.which(name)
        if not executable:
            tools[name] = "missing"
            continue
        try:
            result = subprocess.run(
                [executable, "compose", "version"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            tools[name] = "compose available" if result.returncode == 0 else "compose unavailable"
        except (OSError, subprocess.TimeoutExpired):
            tools[name] = "unavailable"
    file_permissions = {}
    for path in (paths.credentials, paths.context, paths.history):
        try:
            file_permissions[os.path.basename(path)] = oct(stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            file_permissions[os.path.basename(path)] = None
    from . import state as state_module
    state_source = "--state-dir" if state_module._cli_state_dir else next((name for name in ("NAIJ_STATE_DIR", "NITRO_STATE_DIR") if os.environ.get(name, "").strip()), "default")
    reachability = api_reachability(runtime().api_base_url)
    unsafe_permissions = os.name != "nt" and any(int(mode, 8) & 0o077 for mode in [permissions, *file_permissions.values()] if mode)
    unhealthy = (credentials in {"invalid", "missing"} or "expired" in freshness.values()
                 or not reachability["reachable"] or unsafe_permissions or manager == "invalid"
                 or manager_live["status"] not in {"healthy", "not installed"})
    return {
        "exit_status": 1 if unhealthy else 0,
        "platform": f"{sys.platform}/{platform.machine()} Python {platform.python_version()}", "api_reachability": reachability,
        "docker_context": docker_context(),
        "api_source": getattr(runtime(), "api_source", "programmatic"),
        "proxy_source": getattr(runtime(), "proxy_source", "programmatic"),
        "state_source": state_source, "file_permissions": file_permissions,
        "version": __version__, "api_url": public_url(runtime().api_base_url),
        "submission_proxy": runtime().submission_proxy,
        "state_dir": paths.root, "state_permissions": permissions,
        "credentials_file": credentials, "credential_freshness": freshness,
        "freshness_note": "Expiry hints only, not server validation; no tokens refreshed.",
        "manager_config": manager, "manager_live": manager_live,
        "container_tools": tools,
        "guidance": "Use naij login for missing/invalid credentials; naij play manager status for live manager health.",
    }

"""Read-only diagnostics; never loads state through the migrating/hardening path."""
from __future__ import annotations

import json
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


def collect_diagnostics() -> dict:
    paths = inspect_state_paths()
    credentials, _ = _json_state(Path(paths.credentials))
    manager, _ = _json_state(Path(paths.play_manager) / "manager.json")
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
    return {
        "version": __version__, "api_url": public_url(runtime().api_base_url),
        "submission_proxy": runtime().submission_proxy,
        "state_dir": paths.root, "state_permissions": permissions,
        "credentials_file": credentials, "manager_config": manager,
        "container_tools": tools,
        "guidance": "Use naij login for missing/invalid credentials; naij play manager status for live manager health.",
    }

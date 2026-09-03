"""Read-only discovery of known pre-3.0 Play environments."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from .play_protocol import WireError, validate_competition
from .state import resolve_state_paths


ENV_KEYS = {
    "ORGANIZATION_SLUG",
    "COMPETITION_SLUG",
    "NOTEBOOK_IMAGE",
    "PROXY_IMAGE",
    "GPU_REQUESTED",
    "GPU_ENABLED",
}


def _read_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                key, separator, value = line.rstrip("\n").partition("=")
                if separator and key in ENV_KEYS:
                    values[key] = value
    except OSError:
        pass
    return values


def _docker(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(command, 127, "", "")


def discover_legacy_environments(runtime: str = "docker") -> list[dict[str, Any]]:
    root = resolve_state_paths().play
    try:
        entries = sorted(os.scandir(root), key=lambda item: item.name)
    except OSError:
        return []
    manifests: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        compose_path = os.path.join(entry.path, "docker-compose.yml")
        env_path = os.path.join(entry.path, ".env")
        if not os.path.isfile(compose_path) or not os.path.isfile(env_path):
            continue
        values = _read_env(env_path)
        org = values.get("ORGANIZATION_SLUG", "")
        competition = values.get("COMPETITION_SLUG", "")
        try:
            org, competition = validate_competition(org, competition)
        except WireError:
            continue
        project = f"nitro-{org}-{competition}"
        listed = _docker(
            [
                runtime,
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=jupyter-server",
                "--format",
                "{{.ID}}|{{.State}}",
            ]
        )
        container_id = ""
        running = False
        if listed.returncode == 0 and listed.stdout.strip():
            container_id, _, state = listed.stdout.strip().splitlines()[0].partition("|")
            running = state == "running"
        workspace_volume = ""
        workspace_kind = "container-layer"
        if container_id:
            inspected = _docker([runtime, "inspect", container_id])
            try:
                mounts = json.loads(inspected.stdout)[0].get("Mounts") or []
            except (ValueError, IndexError, TypeError, json.JSONDecodeError):
                mounts = []
            for mount in mounts:
                if mount.get("Destination") == "/home/jovyan" and mount.get("Type") == "volume":
                    workspace_volume = str(mount.get("Name") or "")
                    workspace_kind = "volume"
                    break
        manifests.append(
            {
                "reference": f"{org}/{competition}",
                "organization": org,
                "competition": competition,
                "project": project,
                "container_id": container_id,
                "running": running,
                "workspace_kind": workspace_kind,
                "workspace_volume": workspace_volume,
                "notebook_image": values.get("NOTEBOOK_IMAGE", ""),
                "proxy_image": values.get("PROXY_IMAGE", ""),
                "verified": bool(container_id),
            }
        )
    return manifests

"""Host-side installation and lifecycle for the Dockerized Play manager."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .play_manager_client import ManagerClient, ManagerConnectionError
from .play_protocol import (
    API_VERSION,
    BASE_PATH,
    DEFAULT_MANAGER_BIND,
    DEFAULT_MANAGER_IMAGE,
    DEFAULT_MANAGER_PORT,
    MANAGER_IDENTITY,
    parse_version,
)
from .state import atomic_write, ensure_state_dir, load_state, resolve_state_paths


MANAGER_PROJECT = "naij-play-manager"
MANAGER_VOLUME = "naij-play-manager-state"
MANAGER_NETWORK = "naij-play"


@dataclass(frozen=True)
class DockerEndpoint:
    context: str
    host: str
    socket_source: str
    os_type: str


def run_process(
    command: list[str], *, check: bool = True, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Docker is not installed or not on PATH") from exc
    if check and result.returncode:
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(details or f"{command[0]} exited with {result.returncode}")
    return result


def resolve_docker_endpoint() -> DockerEndpoint:
    run_process(["docker", "--version"])
    run_process(["docker", "compose", "version"])
    context = run_process(["docker", "context", "show"]).stdout.strip() or "default"
    inspected = run_process(["docker", "context", "inspect", context])
    try:
        context_data = json.loads(inspected.stdout)[0]
        endpoint = str(context_data["Endpoints"]["docker"]["Host"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not resolve Docker context {context!r}") from exc
    configured = os.environ.get("DOCKER_HOST", "").strip()
    if configured:
        endpoint = configured
    info = run_process(["docker", "info", "--format", "{{json .}}"])
    try:
        os_type = str(json.loads(info.stdout).get("OSType") or "")
    except (TypeError, json.JSONDecodeError):
        os_type = ""
    if os_type and os_type != "linux":
        raise RuntimeError(
            "The Play manager requires Docker Desktop Linux containers; switch from Windows containers and retry"
        )
    if endpoint.startswith("ssh://") or endpoint.startswith("tcp://"):
        raise RuntimeError(
            f"Docker context {context!r} is remote ({endpoint}); the Play manager supports only a local Docker daemon"
        )
    if endpoint.startswith("unix://"):
        source = endpoint.removeprefix("unix://")
        if not os.path.exists(source):
            raise RuntimeError(f"Docker socket does not exist: {source}")
    elif endpoint.startswith("npipe://"):
        if platform.system() != "Windows":
            raise RuntimeError(f"Unsupported Docker endpoint: {endpoint}")
        source = "/var/run/docker.sock"
    else:
        raise RuntimeError(
            f"Unsupported Docker endpoint {endpoint!r}; select a local Unix-socket Docker context"
        )
    return DockerEndpoint(context, endpoint, source, os_type or "linux")


def manager_paths() -> dict[str, str]:
    root = resolve_state_paths().play_manager
    return {
        "root": root,
        "config": os.path.join(root, "manager.json"),
        "compose": os.path.join(root, "compose.json"),
        "token": os.path.join(root, "cli-api-token"),
        "dashboard_token": os.path.join(root, "dashboard-login-token"),
    }


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def load_manager_config() -> dict[str, Any] | None:
    return _read_json(manager_paths()["config"])


def _write_secret(path: str, value: str, *, replace: bool = False) -> None:
    if os.path.exists(path) and not replace:
        os.chmod(path, 0o600)
        return
    atomic_write(path, (value.rstrip("\n") + "\n").encode(), mode=0o600)


def validate_manager_exposure(
    bind: str,
    *,
    tls_cert: str | None,
    tls_key: str | None,
    public_url: str | None,
) -> None:
    loopback = bind in {"127.0.0.1", "::1", "localhost"}
    if loopback:
        return
    if not tls_cert or not tls_key or not public_url:
        raise ValueError(
            "A non-loopback manager requires --tls-cert, --tls-key, and an HTTPS --public-url"
        )
    parsed = urlsplit(public_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("--public-url must be an absolute HTTPS URL")
    for path, label in ((tls_cert, "TLS certificate"), (tls_key, "TLS key")):
        if not os.path.isfile(path):
            raise ValueError(f"{label} does not exist: {path}")


def generate_manager_compose(
    config: dict[str, Any], endpoint: DockerEndpoint
) -> dict[str, Any]:
    paths = manager_paths()
    bind = str(config["bind"])
    port = int(config["port"])
    environment = {
        "NAIJ_MANAGER_BIND": "0.0.0.0",
        "NAIJ_MANAGER_PORT": "51123",
        "NAIJ_MANAGER_PUBLIC_URL": str(config["public_url"]),
        "NAIJ_MANAGER_LAN": "0" if bind in {"127.0.0.1", "::1", "localhost"} else "1",
        "NAIJ_MANAGER_API_TOKEN_FILE": "/run/secrets/api-token",
        "NAIJ_MANAGER_STATE": "/var/lib/naij/manager.db",
        "NAIJ_MANAGER_PROJECTS": "/var/lib/naij/projects",
        "NAIJ_MANAGER_IMAGE": str(config["image"]),
        "NAIJ_MANAGER_NETWORK": MANAGER_NETWORK,
        "NAIJ_MANAGER_DOCKER_CONTEXT": endpoint.context,
    }
    secrets_config: dict[str, Any] = {
        "api-token": {"file": paths["token"]},
    }
    service_secrets: list[str] = ["api-token"]
    volumes = [
        f"{MANAGER_VOLUME}:/var/lib/naij",
        f"{endpoint.socket_source}:/var/run/docker.sock",
    ]
    if config.get("dashboard_token"):
        environment["NAIJ_MANAGER_DASHBOARD_TOKEN_FILE"] = "/run/secrets/dashboard-token"
        secrets_config["dashboard-token"] = {"file": paths["dashboard_token"]}
        service_secrets.append("dashboard-token")
    if config.get("tls_cert"):
        environment["NAIJ_MANAGER_TLS_CERT"] = "/run/tls/cert.pem"
        environment["NAIJ_MANAGER_TLS_KEY"] = "/run/tls/key.pem"
        volumes.extend(
            [
                f"{config['tls_cert']}:/run/tls/cert.pem:ro",
                f"{config['tls_key']}:/run/tls/key.pem:ro",
            ]
        )
    labels = {
        "org.nitro-ai.naij.play.owner": MANAGER_IDENTITY,
        "org.nitro-ai.naij.play.role": "manager",
        "org.nitro-ai.naij.play.schema": "1",
        "org.nitro-ai.naij.play.api": str(API_VERSION),
    }
    health_url = (
        "https://127.0.0.1:51123/nitro/api/v1/health"
        if config.get("tls_cert")
        else "http://127.0.0.1:51123/nitro/api/v1/health"
    )
    health_script = f"import urllib.request; urllib.request.urlopen({health_url!r}, timeout=2"
    if config.get("tls_cert"):
        health_script = (
            "import ssl, urllib.request; urllib.request.urlopen("
            f"{health_url!r}, context=ssl._create_unverified_context(), timeout=2"
        )
    health_script += ")"
    return {
        "name": MANAGER_PROJECT,
        "services": {
            "manager": {
                "image": config["image"],
                "restart": "unless-stopped",
                "environment": environment,
                "ports": [f"{bind}:{port}:51123"],
                "volumes": volumes,
                "secrets": service_secrets,
                "networks": ["nitro"],
                "labels": labels,
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        health_script,
                    ],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 12,
                    "start_period": "5s",
                },
            }
        },
        "volumes": {MANAGER_VOLUME: {"name": MANAGER_VOLUME, "labels": labels}},
        "networks": {
            "nitro": {"name": MANAGER_NETWORK, "labels": labels}
        },
        "secrets": secrets_config,
    }


def _compose_command(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        MANAGER_PROJECT,
        "--file",
        manager_paths()["compose"],
        *args,
    ]


def _port_in_use(bind: str, port: int) -> bool:
    host = "127.0.0.1" if bind in {"0.0.0.0", "localhost"} else bind
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def _verify_manager(timeout: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client = ManagerClient.from_state()
            health = client.health()
            info = client.info()
            if health.get("status") == "healthy":
                verify_manager_info(info)
                return info
        except (ManagerConnectionError, RuntimeError) as exc:
            last_error = exc
        time.sleep(1)
    logs = run_process(_compose_command("logs", "--tail", "40"), check=False)
    details = (logs.stdout or logs.stderr or str(last_error or "no response")).strip()
    raise RuntimeError(f"Play manager did not become healthy\n{details}")


def verify_manager_info(info: dict[str, Any]) -> None:
    if info.get("identity") != MANAGER_IDENTITY:
        raise RuntimeError(
            "The configured port is not a Nitro Play manager; choose another --port"
        )
    api = int(info.get("api_version") or 0)
    if api != API_VERSION:
        direction = "newer" if api > API_VERSION else "older"
        raise RuntimeError(
            f"Play manager API v{api} is {direction} than this CLI supports (v{API_VERSION}); run `naij play manager update`"
        )
    minimum = str(info.get("minimum_cli_version") or "0.0.0")
    if parse_version(__version__) < parse_version(minimum):
        raise RuntimeError(
            f"Play manager requires NAIJ {minimum} or newer; update the CLI"
        )


def install_manager(
    *,
    bind: str = DEFAULT_MANAGER_BIND,
    port: int = DEFAULT_MANAGER_PORT,
    image: str = DEFAULT_MANAGER_IMAGE,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    public_url: str | None = None,
    update: bool = False,
) -> dict[str, Any]:
    validate_manager_exposure(
        bind, tls_cert=tls_cert, tls_key=tls_key, public_url=public_url
    )
    endpoint = resolve_docker_endpoint()
    paths = manager_paths()
    ensure_state_dir()
    os.makedirs(paths["root"], mode=0o700, exist_ok=True)
    os.chmod(paths["root"], 0o700)
    old_config = load_manager_config()
    old_compose = None
    try:
        with open(paths["compose"], "rb") as stream:
            old_compose = stream.read()
    except OSError:
        pass

    scheme = "https" if tls_cert else "http"
    host = "localhost" if bind in {"127.0.0.1", "::1"} else bind
    public_url = (public_url or f"{scheme}://{host}:{port}").rstrip("/")
    if _port_in_use(bind, port):
        try:
            existing = ManagerClient(public_url).info()
            verify_manager_info(existing)
        except Exception as exc:
            raise RuntimeError(
                f"Port {bind}:{port} is occupied by another service; choose a different --port"
            ) from exc
        if not update:
            return existing

    _write_secret(paths["token"], secrets.token_urlsafe(48))
    lan = bind not in {"127.0.0.1", "::1", "localhost"}
    if lan:
        _write_secret(paths["dashboard_token"], secrets.token_urlsafe(32))
        print(
            "WARNING: the Play dashboard will be reachable from the network; keep the TLS key and dashboard login token private.",
            file=sys.stderr,
        )
    config = {
        "schema": 1,
        "image": image,
        "bind": bind,
        "port": port,
        "public_url": public_url,
        "tls_cert": os.path.abspath(tls_cert) if tls_cert else None,
        "tls_key": os.path.abspath(tls_key) if tls_key else None,
        "dashboard_token": bool(lan),
        "docker_context": endpoint.context,
        "docker_host": endpoint.host,
    }
    compose = generate_manager_compose(config, endpoint)
    image_present = run_process(["docker", "image", "inspect", image], check=False).returncode == 0
    if update or not image_present:
        run_process(["docker", "pull", image])
    atomic_write(
        paths["config"], (json.dumps(config, indent=2, sort_keys=True) + "\n").encode()
    )
    atomic_write(
        paths["compose"], (json.dumps(compose, indent=2, sort_keys=True) + "\n").encode()
    )
    try:
        run_process(_compose_command("up", "-d", "--remove-orphans"))
        info = _verify_manager()
        sync_manager_credentials(required=False)
        from .play_legacy import discover_legacy_environments

        manifests = discover_legacy_environments()
        if manifests:
            ManagerClient.from_state().adopt_legacy(manifests)
        return info
    except Exception:
        if update and old_config is not None and old_compose is not None:
            atomic_write(
                paths["config"],
                (json.dumps(old_config, indent=2, sort_keys=True) + "\n").encode(),
            )
            atomic_write(paths["compose"], old_compose)
            run_process(_compose_command("up", "-d", "--remove-orphans"), check=False)
        raise


def manager_status() -> dict[str, Any]:
    config = load_manager_config()
    if not config:
        return {"installed": False, "status": "missing"}
    try:
        client = ManagerClient.from_state()
        info = client.info()
        health = client.health()
        verify_manager_info(info)
        return {"installed": True, "config": config, "info": info, "health": health}
    except Exception as exc:
        return {
            "installed": True,
            "config": config,
            "status": "unavailable",
            "error": str(exc),
        }


def manager_container_exists() -> bool:
    return bool(
        run_process(_compose_command("ps", "--all", "--quiet", "manager")).stdout.strip()
    )


def manager_compose_action(action: str) -> None:
    if not load_manager_config():
        raise RuntimeError("Play manager is not installed")
    if action not in {"start", "stop", "restart"}:
        raise ValueError(f"Unsupported manager action: {action}")
    run_process(_compose_command(action))
    if action != "stop":
        _verify_manager()


def uninstall_manager() -> None:
    if not load_manager_config():
        return
    run_process(_compose_command("down", "--remove-orphans"))


def purge_manager_state(*, force: bool = False) -> None:
    if not force:
        raise RuntimeError("Purging manager-private state requires --force")
    uninstall_manager()
    inspected = run_process(
        ["docker", "volume", "inspect", MANAGER_VOLUME], check=False
    )
    if inspected.returncode == 0:
        try:
            labels = json.loads(inspected.stdout)[0].get("Labels") or {}
        except (ValueError, IndexError, TypeError, json.JSONDecodeError):
            labels = {}
        if labels.get("org.nitro-ai.naij.play.owner") != MANAGER_IDENTITY:
            raise RuntimeError(
                f"Refusing to remove volume {MANAGER_VOLUME}: ownership label is missing"
            )
        run_process(["docker", "volume", "rm", MANAGER_VOLUME])


def normalized_credentials(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "access_token": str(value.get("access_token") or value.get("accessToken") or ""),
        "refresh_token": str(value.get("refresh_token") or value.get("refreshToken") or ""),
        "access_token_exp": value.get("access_token_exp"),
        "refresh_token_exp": value.get("refresh_token_exp"),
        "username": str(value.get("username") or ""),
        "api_base_url": "https://judge.nitro-ai.org/api",
    }


def sync_manager_credentials(*, required: bool = True) -> bool:
    saved = load_state()
    if not saved:
        if required:
            raise RuntimeError("Login synchronization required; run `naij login`")
        return False
    credentials = normalized_credentials(saved)
    if not credentials["access_token"] or not credentials["refresh_token"]:
        if required:
            raise RuntimeError("Login synchronization required; run `naij login`")
        return False
    ManagerClient.from_state().put_credentials(credentials)
    return True

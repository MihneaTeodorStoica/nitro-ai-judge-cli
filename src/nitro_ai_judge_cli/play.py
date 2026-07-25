"""Local Docker Compose lifecycle for Nitro contestant environments."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error as urllib_error
import urllib.request as urllib_request

from . import state
from .ui import Spinner


PLAY_DEFAULT_PORT = 8888
PLAY_PROXY_PORT = 9000
PLAY_WAIT_TIMEOUT = 120
PLAY_ACTIONS = {"up", "start", "stop", "restart", "down", "logs", "ps", "status"}
# Optional process-local override retained for embedders and isolated tests.
PLAY_STATE_DIR: str | None = None


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
    root = PLAY_STATE_DIR
    if root is None:
        root = state.ensure_state_dir().play
    return os.path.join(
        root,
        f"{play_slug(org)}-{play_slug(comp)}",
    )


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
    except FileNotFoundError as exc:
        raise RuntimeError("Docker is not installed or not on PATH") from exc
    if check and result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(output or f"{cmd[0]} exited with {result.returncode}")
    return result


def ensure_docker_ready() -> None:
    run_process(["docker", "--version"])
    run_process(["docker", "compose", "version"])
    run_process(["docker", "info"])


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


def port_is_free(bind: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((bind, port))
        except OSError:
            return False
    return True


def allocate_port(bind: str, preferred: int, explicit: int | None) -> int:
    if explicit is not None:
        if not port_is_free(bind, explicit):
            raise RuntimeError(f"Host port {explicit} is already in use")
        return explicit
    port = preferred
    while not port_is_free(bind, port):
        port += 1
        if port > 65535:
            raise RuntimeError("No free host port found")
    return port


def read_play_env(org: str, comp: str) -> dict[str, str]:
    path = os.path.join(play_workdir(org, comp), ".env")
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                key, separator, value = line.rstrip("\n").partition("=")
                if separator and key:
                    values[key] = value
    except FileNotFoundError:
        pass
    return values


def play_images(org: str, comp: str) -> tuple[str, str]:
    return (
        f"nitroai/{org}-{comp}-notebook:latest",
        f"nitroai/{org}-{comp}-judge-proxy:latest",
    )


def ensure_play_images(
    images: tuple[str, str], policy: str, *, quiet: bool = False
) -> None:
    if policy not in {"always", "missing", "never"}:
        raise ValueError(f"invalid pull policy: {policy}")

    present: dict[str, bool] = {}
    for image in images:
        present[image] = (
            run_process(["docker", "image", "inspect", image], check=False).returncode
            == 0
        )

    missing = [image for image in images if not present[image]]
    if policy == "never" and missing:
        raise RuntimeError(f"Image is missing and --pull=never: {missing[0]}")

    required = list(images) if policy == "always" else missing if policy == "missing" else []
    total = len(required)
    for index, image in enumerate(required, 1):
        spinner = (
            None
            if quiet
            else Spinner(
                f"Pulling image {index}/{total}: {image}", stream=sys.stdout
            ).start()
        )
        try:
            run_process(["docker", "pull", image])
        except Exception:
            if spinner is not None:
                spinner.stop()
            raise
        if spinner is not None:
            spinner.stop()
            print(f"Pulled image: {image}")


def resolve_play_gpu(
    image: str, requested: bool | None, *, quiet: bool = False
) -> tuple[bool, str]:
    if requested is False:
        return False, "disabled"
    probe = run_process(
        [
            "docker",
            "run",
            "--rm",
            "--gpus",
            "all",
            "--entrypoint",
            "nvidia-smi",
            image,
        ],
        check=False,
    )
    if probe.returncode == 0:
        return True, "available"
    reason = (
        (probe.stderr or probe.stdout or "GPU execution unavailable")
        .strip()
        .splitlines()[-1]
    )
    if requested is True:
        raise RuntimeError(f"GPU requested but unavailable: {reason}")
    if not quiet:
        print(f"GPU unavailable; using CPU ({reason})")
    return False, reason


def play_workspace_volume(org: str, comp: str) -> str:
    return f"{play_project_name(org, comp)}_workspace"


def describe_play_state(org: str, comp: str) -> str:
    workdir = play_workdir(org, comp)
    if not os.path.exists(os.path.join(workdir, "docker-compose.yml")):
        return "not configured"
    try:
        result = run_process(
            [*play_compose_base_cmd(org, comp), "ps", "--format", "json"],
            cwd=workdir,
            check=False,
        )
    except RuntimeError:
        return "configured (Docker unavailable)"
    if result.returncode != 0:
        return "configured (state unavailable)"
    try:
        parsed = json.loads(result.stdout or "[]")
        containers = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        containers = [json.loads(line) for line in result.stdout.splitlines() if line]
    states = sorted({str(item.get("State", "unknown")).lower() for item in containers})
    return ", ".join(states) if states else "not created"


def load_play_logs(org: str, comp: str, *, tail: int = 80) -> str:
    """Return recent Compose logs without printing."""
    workdir = play_workdir(org, comp)
    if not os.path.exists(os.path.join(workdir, "docker-compose.yml")):
        return ""
    result = run_process(
        [*play_compose_base_cmd(org, comp), "logs", "--tail", str(tail)],
        cwd=workdir,
        check=False,
    )
    return "\n".join(
        part
        for part in (
            (getattr(result, "stdout", "") or "").strip(),
            (getattr(result, "stderr", "") or "").strip(),
        )
        if part
    )


def load_play_ps(org: str, comp: str) -> str:
    """Return the current Compose process table without printing."""
    workdir = play_workdir(org, comp)
    if not os.path.exists(os.path.join(workdir, "docker-compose.yml")):
        return "No play environment found."
    result = run_process(
        [*play_compose_base_cmd(org, comp), "ps"],
        cwd=workdir,
        check=False,
    )
    return "\n".join(
        part
        for part in (
            (getattr(result, "stdout", "") or "").strip(),
            (getattr(result, "stderr", "") or "").strip(),
        )
        if part
    )


def load_play_status(
    org: str, comp: str, *, logs: int = 0
) -> dict[str, str | None]:
    """Return the saved and live play state for CLI and TUI renderers."""
    env = read_play_env(org, comp)
    current_state = describe_play_state(org, comp)
    if current_state in {"created", "exited"}:
        current_state = "stopped"
    bind = env.get("BIND_ADDRESS", "127.0.0.1")
    host = "localhost" if bind in {"127.0.0.1", "0.0.0.0"} else bind
    jupyter_port = env.get("JUPYTER_PORT")
    proxy_port = env.get("PROXY_PORT")
    requested = env.get("GPU_REQUESTED")
    effective = "gpu" if env.get("GPU_ENABLED") == "1" else "cpu"
    return {
        "state": current_state,
        "jupyter_url": f"http://{host}:{jupyter_port}" if jupyter_port else None,
        "proxy_url": f"http://{host}:{proxy_port}" if proxy_port else None,
        "gpu": f"{requested} (effective {effective})" if requested else None,
        "images": ", ".join(
            item
            for item in (
                env.get("NOTEBOOK_IMAGE"),
                env.get("PROXY_IMAGE"),
            )
            if item
        )
        or None,
        "workspace": play_workspace_volume(org, comp),
        "workdir": play_workdir(org, comp),
        "logs": load_play_logs(org, comp, tail=logs) if logs else None,
    }


def change_play_state(org: str, comp: str, action: str) -> None:
    """Start or restart existing containers without terminal output."""
    if action not in {"start", "restart"}:
        raise ValueError(f"Unsupported play state change: {action}")
    workdir = play_workdir(org, comp)
    if not os.path.exists(os.path.join(workdir, "docker-compose.yml")):
        raise RuntimeError(
            f"No play environment found; use 'play up {org}/{comp}'"
        )
    ensure_docker_ready()
    if action == "start" and not run_process(
        [*play_compose_base_cmd(org, comp), "ps", "-a", "-q"],
        cwd=workdir,
        check=False,
    ).stdout.strip():
        raise RuntimeError("No containers exist; use 'play up' instead")
    run_process([*play_compose_base_cmd(org, comp), action], cwd=workdir)


def migrate_legacy_workspace(org: str, comp: str, image: str) -> None:
    if not os.path.exists(os.path.join(play_workdir(org, comp), "docker-compose.yml")):
        return
    base = play_compose_base_cmd(org, comp)
    legacy = run_process(
        [*base, "ps", "--all", "-q", "jupyter-server"],
        cwd=play_workdir(org, comp),
        check=False,
    )
    container = legacy.stdout.strip()
    if not container:
        return
    inspected = run_process(["docker", "inspect", container], check=False)
    if inspected.returncode != 0:
        return
    data = json.loads(inspected.stdout)[0]
    volume = play_workspace_volume(org, comp)
    if any(
        mount.get("Destination") == "/home/jovyan" and mount.get("Name") == volume
        for mount in data.get("Mounts", [])
    ):
        return
    was_running = bool(data.get("State", {}).get("Running"))
    temporary = f"{play_project_name(org, comp)}-workspace-migration"
    run_process(["docker", "stop", container])
    try:
        run_process(["docker", "volume", "create", volume])
        run_process(
            [
                "docker",
                "create",
                "--name",
                temporary,
                "-v",
                f"{volume}:/home/jovyan",
                "--entrypoint",
                "/bin/true",
                image,
            ]
        )
        run_process(["docker", "start", "--attach", temporary])
        run_process(
            [
                "docker",
                "cp",
                "--archive",
                f"{container}:/home/jovyan/.",
                f"{temporary}:/home/jovyan",
            ]
        )
        run_process(["docker", "rm", temporary])
    except RuntimeError:
        run_process(["docker", "rm", "-f", temporary], check=False)
        run_process(["docker", "volume", "rm", volume], check=False)
        if was_running:
            run_process(["docker", "start", container], check=False)
        raise


def write_play_files(
    org: str,
    comp: str,
    port: int,
    proxy_port: int,
    bind: str,
    gpu_requested: str,
    gpu: bool,
    images: tuple[str, str],
) -> str:
    state.ensure_state_dir()
    workdir = play_workdir(org, comp)
    secrets_dir = os.path.join(workdir, "secrets")
    os.makedirs(secrets_dir, exist_ok=True)
    secret_path = os.path.join(secrets_dir, "session_whitelist_bypass_key")
    if not os.path.exists(secret_path):
        open(secret_path, "a", encoding="utf-8").close()
    os.chmod(secret_path, 0o600)

    env = "\n".join(
        [
            "JUDGE_BASE_URL=https://judge.nitro-ai.org/api",
            f"ORGANIZATION_SLUG={org}",
            f"COMPETITION_SLUG={comp}",
            f"JUPYTER_PORT={port}",
            f"PROXY_PORT={proxy_port}",
            f"BIND_ADDRESS={bind}",
            f"GPU_REQUESTED={gpu_requested}",
            f"GPU_ENABLED={'1' if gpu else '0'}",
            f"NOTEBOOK_IMAGE={images[0]}",
            f"PROXY_IMAGE={images[1]}",
            "JUPYTER_BASE_URL=http://jupyter-server:8888/",
            "SESSION_WHITELIST_BYPASS_KEY_FILE=/run/secrets/session_whitelist_bypass_key",
            "PROXY_DISABLE_CACHE=1",
            "PROXY_PREJUDGING_TIMEOUT_S=300",
            "",
        ]
    )
    env_path = os.path.join(workdir, ".env")
    with open(env_path, "w", encoding="utf-8") as stream:
        stream.write(env)
    os.chmod(env_path, 0o600)

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
    image: ${{NOTEBOOK_IMAGE}}
    ports:
      - "${{BIND_ADDRESS:-127.0.0.1}}:${{JUPYTER_PORT:-8888}}:8888"
    volumes:
      - workspace:/home/jovyan
    environment:
      PROXY_URL: "http://submission-proxy:${{PROXY_PORT:-9000}}"
      PROXY_URL_CLIENT: "http://localhost:${{PROXY_PORT:-9000}}"
    cap_add:
      - NET_ADMIN
    depends_on:
      - submission-proxy
{gpu_block}  submission-proxy:
    image: ${{PROXY_IMAGE}}
    ports:
      - "${{BIND_ADDRESS:-127.0.0.1}}:${{PROXY_PORT:-9000}}:9000"
    environment:
      JUDGE_BASE_URL: "${{JUDGE_BASE_URL}}"
      ORGANIZATION_SLUG: "${{ORGANIZATION_SLUG}}"
      COMPETITION_SLUG: "${{COMPETITION_SLUG}}"
      PROXY_PORT: "${{PROXY_PORT:-9000}}"
      JUPYTER_BASE_URL: "http://jupyter-server:8888/"
      SESSION_WHITELIST_BYPASS_KEY_FILE: "/run/secrets/session_whitelist_bypass_key"
      PROXY_DISABLE_CACHE: "1"
      PROXY_PREJUDGING_TIMEOUT_S: "300"
    secrets:
      - session_whitelist_bypass_key
    cap_add:
      - SYS_ADMIN
    security_opt:
      - seccomp:unconfined
      - apparmor:unconfined
secrets:
  session_whitelist_bypass_key:
    file: ./secrets/session_whitelist_bypass_key

volumes:
  workspace:
"""
    with open(
        os.path.join(workdir, "docker-compose.yml"), "w", encoding="utf-8"
    ) as stream:
        stream.write(compose)
    return workdir


def service_url_ready(url: str) -> bool:
    try:
        with urllib_request.urlopen(url, timeout=1):
            return True
    except urllib_error.HTTPError:
        return True
    except (OSError, urllib_error.URLError):
        return False


def wait_for_play(
    org: str, comp: str, bind: str, port: int, proxy_port: int, timeout: int
) -> None:
    host = "127.0.0.1" if bind == "0.0.0.0" else bind
    urls = [f"http://{host}:{port}", f"http://{host}:{proxy_port}"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = run_process(
            [*play_compose_base_cmd(org, comp), "ps", "--format", "json"],
            cwd=play_workdir(org, comp),
            check=False,
        )
        if current.returncode != 0 or any(
            token in current.stdout.lower()
            for token in ('"state":"exited"', '"state": "exited"', '"state":"dead"')
        ):
            break
        if all(service_url_ready(url) for url in urls):
            return
        time.sleep(1)
    base = play_compose_base_cmd(org, comp)
    current = run_process([*base, "ps"], cwd=play_workdir(org, comp), check=False)
    logs = run_process(
        [*base, "logs", "--tail", "30"], cwd=play_workdir(org, comp), check=False
    )
    details = "\n".join(
        part
        for part in (current.stdout.strip(), logs.stdout.strip(), logs.stderr.strip())
        if part
    )
    raise RuntimeError(
        f"Play services did not become ready within {timeout}s\n{details}"
    )


def cmd_play_up(
    org: str,
    comp: str,
    *,
    gpu: bool | None,
    port: int | None,
    proxy_port: int | None,
    bind: str,
    pull: str,
    wait_timeout: int,
    quiet: bool = False,
) -> int:
    ensure_docker_ready()
    saved = read_play_env(org, comp)
    running = play_jupyter_running(org, comp)
    if (
        running
        and port is None
        and proxy_port is None
        and bind == saved.get("BIND_ADDRESS", bind)
    ):
        selected_port = int(saved.get("JUPYTER_PORT", PLAY_DEFAULT_PORT))
        selected_proxy_port = int(saved.get("PROXY_PORT", PLAY_PROXY_PORT))
    else:
        preferred_port = (
            int(saved.get("JUPYTER_PORT", PLAY_DEFAULT_PORT)) if port is None else port
        )
        preferred_proxy = (
            int(saved.get("PROXY_PORT", PLAY_PROXY_PORT))
            if proxy_port is None
            else proxy_port
        )
        selected_port = allocate_port(bind, preferred_port, port)
        selected_proxy_port = allocate_port(bind, preferred_proxy, proxy_port)
        if selected_proxy_port == selected_port:
            if proxy_port is not None:
                raise RuntimeError("--port and --proxy-port must be different")
            selected_proxy_port = allocate_port(bind, selected_proxy_port + 1, None)
    images = play_images(org, comp)
    ensure_play_images(images, pull, quiet=quiet)
    effective_gpu, _ = resolve_play_gpu(images[0], gpu, quiet=quiet)
    migrate_legacy_workspace(org, comp, images[0])
    requested = "auto" if gpu is None else ("gpu" if gpu else "cpu")
    workdir = write_play_files(
        org,
        comp,
        selected_port,
        selected_proxy_port,
        bind,
        requested,
        effective_gpu,
        images,
    )
    base_cmd = play_compose_base_cmd(org, comp)
    run_process([*base_cmd, "up", "-d"], cwd=workdir)
    wait_for_play(org, comp, bind, selected_port, selected_proxy_port, wait_timeout)
    if not quiet:
        print(f"Started {org}/{comp}")
        host = "localhost" if bind in {"127.0.0.1", "0.0.0.0"} else bind
        print(f"Jupyter: http://{host}:{selected_port}")
        print(f"Proxy: http://{host}:{selected_proxy_port}")
        print(f"State: {workdir}")
    return 0


def cmd_play_stop(org: str, comp: str, *, quiet: bool = False) -> int:
    ensure_docker_ready()
    workdir = play_workdir(org, comp)
    if not os.path.exists(os.path.join(workdir, "docker-compose.yml")):
        if not quiet:
            print(f"No play environment found: {workdir}")
        return 0
    run_process([*play_compose_base_cmd(org, comp), "stop"], cwd=workdir)
    if not quiet:
        print(f"Stopped {org}/{comp}")
    return 0


def cmd_play_down(
    org: str,
    comp: str,
    *,
    volumes: bool = False,
    force: bool = False,
    quiet: bool = False,
) -> int:
    ensure_docker_ready()
    workdir = play_workdir(org, comp)
    compose_file = os.path.join(workdir, "docker-compose.yml")
    if not os.path.exists(compose_file):
        shutil.rmtree(workdir, ignore_errors=True)
        if not quiet:
            print(f"No play environment found: {workdir}")
        return 0
    if volumes and not force:
        if not sys.stdin.isatty():
            raise RuntimeError("down --volumes requires --force in non-interactive use")
        if (
            input(f"Delete all workspace data for {org}/{comp}? [y/N] ")
            .strip()
            .lower()
            != "y"
        ):
            print("Aborted.")
            return 1
    command = [*play_compose_base_cmd(org, comp), "down", "--remove-orphans"]
    if volumes:
        command.append("--volumes")
    run_process(command, cwd=workdir)
    if not quiet:
        print(f"Removed {org}/{comp}")
    return 0


def cmd_play_logs(org: str, comp: str, *, follow: bool = False) -> int:
    ensure_docker_ready()
    workdir = play_workdir(org, comp)
    if not os.path.exists(os.path.join(workdir, "docker-compose.yml")):
        print(f"No play environment found: {workdir}")
        return 1
    if not follow:
        print(load_play_logs(org, comp), end="")
        return 0
    try:
        return subprocess.run(
            [*play_compose_base_cmd(org, comp), "logs", "-f"],
            cwd=workdir,
        ).returncode
    except FileNotFoundError:
        print("Error: Docker is not installed or not on PATH")
        return 1


def cmd_play(args: argparse.Namespace) -> int:
    try:
        org, comp = parse_competition_ref(args.competition)
        if args.play_action in {"start", "restart"}:
            change_play_state(org, comp, args.play_action)
            return 0
        if args.play_action == "stop":
            return cmd_play_stop(org, comp)
        if args.play_action == "logs":
            return cmd_play_logs(org, comp, follow=args.follow)
        if args.play_action == "down":
            return cmd_play_down(org, comp, volumes=args.volumes, force=args.force)
        if args.play_action == "ps":
            workdir = play_workdir(org, comp)
            compose_file = os.path.join(workdir, "docker-compose.yml")
            if not os.path.exists(compose_file):
                raise RuntimeError(
                    f"No play environment found; use 'play up {org}/{comp}'"
                )
            ensure_docker_ready()
            result = run_process([*play_compose_base_cmd(org, comp), "ps"], cwd=workdir)
            print(result.stdout, end="")
            return 0
        if args.play_action == "status":
            status = load_play_status(org, comp)
            print(f"Contest: {org}/{comp}")
            print(f"State: {status['state']}")
            if status["jupyter_url"]:
                print(f"Jupyter: {status['jupyter_url']}")
                print(f"Proxy: {status['proxy_url']}")
                print(f"GPU: {status['gpu']}")
                print(f"Images: {status['images']}")
            print(f"Workspace: {status['workspace']}")
            print(f"Files: {status['workdir']}")
            return 0
        return cmd_play_up(
            org,
            comp,
            gpu=args.gpu,
            port=args.port,
            proxy_port=args.proxy_port,
            bind=args.bind,
            pull=args.pull,
            wait_timeout=args.wait_timeout,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1


def normalize_play_argv(argv: list[str]) -> list[str]:
    if not argv or argv[0] not in PLAY_ACTIONS:
        return ["up", *argv]
    return list(argv)


def play_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def positive_seconds(value: str) -> int:
    seconds = int(value)
    if seconds < 1:
        raise argparse.ArgumentTypeError("timeout must be at least 1 second")
    return seconds


def populate_play_actions(actions: argparse._SubParsersAction) -> None:
    up = actions.add_parser("up", help="Create or recreate and start the environment")
    up.add_argument("competition", nargs="*", help="[<org>/<comp> | <org> <comp>]")
    gpu = up.add_mutually_exclusive_group()
    gpu.add_argument(
        "--gpu", dest="gpu", action="store_true", help="Require working GPU access"
    )
    gpu.add_argument(
        "--no-gpu", dest="gpu", action="store_false", help="Disable GPU access"
    )
    up.set_defaults(gpu=None)
    up.add_argument("--port", type=play_port, help="Host Jupyter port")
    up.add_argument("--proxy-port", type=play_port, help="Host submission proxy port")
    up.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Published bind address (default: 127.0.0.1)",
    )
    up.add_argument("--pull", choices=("always", "missing", "never"), default="missing")
    up.add_argument("--wait-timeout", type=positive_seconds, default=PLAY_WAIT_TIMEOUT)

    for action, help_text in (
        ("start", "Start existing containers"),
        ("stop", "Stop containers without removing them"),
        ("restart", "Restart containers"),
        ("ps", "Show Compose container state"),
        ("status", "Show saved play configuration"),
    ):
        command = actions.add_parser(action, help=help_text)
        command.add_argument(
            "competition", nargs="*", help="[<org>/<comp> | <org> <comp>]"
        )

    down = actions.add_parser("down", help="Remove containers and network")
    down.add_argument("competition", nargs="*", help="[<org>/<comp> | <org> <comp>]")
    down.add_argument(
        "--volumes", action="store_true", help="Also delete workspace data"
    )
    down.add_argument(
        "--force", action="store_true", help="Skip destructive confirmation"
    )

    logs = actions.add_parser("logs", help="Print Compose logs")
    logs.add_argument("competition", nargs="*", help="[<org>/<comp> | <org> <comp>]")
    logs.add_argument("-f", "--follow", action="store_true", help="Follow log output")


def build_play_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="naij play", add_help=add_help)
    actions = parser.add_subparsers(dest="play_action", required=True, metavar="ACTION")
    populate_play_actions(actions)
    return parser


def add_play_parser(subparsers: argparse._SubParsersAction) -> None:
    play = subparsers.add_parser(
        "play",
        help="Launch or manage a past contest locally with Docker",
        description="Launch or manage a persistent contest workspace with Docker Compose.",
    )
    actions = play.add_subparsers(dest="play_action", required=True, metavar="ACTION")
    populate_play_actions(actions)

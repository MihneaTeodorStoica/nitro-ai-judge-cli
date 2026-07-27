"""CLI command layer for the Dockerized Nitro Play manager."""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from typing import Any

from .play_manager_client import ManagerClient, ManagerConnectionError
from .play_manager_lifecycle import (
    install_manager,
    load_manager_config,
    manager_compose_action,
    manager_status,
    purge_manager_state,
    sync_manager_credentials,
    uninstall_manager,
    verify_manager_info,
)
from .play_protocol import (
    DEFAULT_MANAGER_BIND,
    DEFAULT_MANAGER_IMAGE,
    DEFAULT_MANAGER_PORT,
    WireError,
    validate_competition,
)


PLAY_WAIT_TIMEOUT = 120
PLAY_ACTIONS = {
    "pull",
    "play",
    "up",
    "start",
    "stop",
    "restart",
    "recreate",
    "down",
    "delete-container",
    "delete-workspace",
    "logs",
    "ps",
    "status",
    "open",
    "manager",
}


def parse_competition_ref(parts: list[str]) -> tuple[str, str]:
    if len(parts) == 1 and parts[0].count("/") == 1:
        org, competition = parts[0].split("/", 1)
    elif len(parts) == 2:
        org, competition = parts
    else:
        raise ValueError("competition must be <org>/<comp> or <org> <comp>")
    return validate_competition(org, competition)


def _install_or_repair_manager() -> dict[str, Any]:
    config = load_manager_config()
    if not config:
        return install_manager()
    return install_manager(
        bind=str(config.get("bind") or DEFAULT_MANAGER_BIND),
        port=int(config.get("port") or DEFAULT_MANAGER_PORT),
        image=str(config.get("image") or DEFAULT_MANAGER_IMAGE),
        tls_cert=config.get("tls_cert"),
        tls_key=config.get("tls_key"),
        public_url=config.get("public_url"),
        update=True,
    )


def _client(*, yes: bool = False, interactive: bool = True) -> ManagerClient:
    try:
        client = ManagerClient.from_state()
        info = client.info()
        verify_manager_info(info)
        return client
    except (ManagerConnectionError, WireError, RuntimeError) as exc:
        if yes:
            _install_or_repair_manager()
            return ManagerClient.from_state()
        if interactive and sys.stdin.isatty() and sys.stdout.isatty():
            answer = input(
                f"Play manager is unavailable ({exc}). Install or repair it now? [y/N] "
            ).strip().lower()
            if answer == "y":
                _install_or_repair_manager()
                return ManagerClient.from_state()
        raise ManagerConnectionError(
            f"{exc}\nRun: naij play manager install --yes"
        ) from exc


def _progress(event: dict[str, Any]) -> None:
    message = str(event.get("message") or "")
    if message:
        print(message)


def perform_play_action(
    org: str,
    competition: str,
    action: str,
    *,
    client: ManagerClient | None = None,
    quiet: bool = False,
    yes: bool = False,
    timeout: int = 600,
    **options: Any,
) -> dict[str, Any]:
    client = client or _client(yes=yes)
    accepted = client.action(org, competition, action, **options)
    operation_id = str(accepted["operation_id"])
    operation = client.wait_operation(
        operation_id,
        timeout=timeout,
        progress=None if quiet else _progress,
    )
    return operation.get("result") or {}


def load_play_status(
    org: str,
    competition: str,
    *,
    logs: int = 0,
    client: ManagerClient | None = None,
) -> dict[str, Any]:
    client = client or _client(interactive=False)
    snapshot = client.competition(org, competition)
    base = client.base_url
    result = {
        **snapshot,
        "state": snapshot.get("workspace_state") or "missing",
        "jupyter_url": f"{base}{snapshot.get('jupyter_url')}" if snapshot.get("jupyter_url") else None,
        "proxy_url": f"{base}{snapshot.get('proxy_url')}" if snapshot.get("proxy_url") else None,
        "gpu": snapshot.get("gpu") or "managed by Play manager",
        "images": ", ".join(
            str(item.get("name"))
            for item in (snapshot.get("images") or {}).values()
            if isinstance(item, dict) and item.get("name")
        ) or None,
        "workdir": "manager-private",
        "logs": None,
        "manager_url": f"{base}/nitro/",
        "manager_available": True,
    }
    if logs:
        result["logs"] = client.logs(org, competition, tail=logs).get("logs")
    return result


def load_play_logs(
    org: str,
    competition: str,
    *,
    tail: int = 80,
    client: ManagerClient | None = None,
) -> str:
    return str((client or _client(interactive=False)).logs(org, competition, tail=tail).get("logs") or "")


def load_play_ps(
    org: str, competition: str, *, client: ManagerClient | None = None
) -> str:
    snapshot = (client or _client(interactive=False)).competition(org, competition)
    return "\n".join(
        (
            f"REFERENCE  {snapshot.get('reference', f'{org}/{competition}')}",
            f"WORKSPACE  {snapshot.get('workspace_state', 'unknown')}",
            f"HEALTH     {snapshot.get('service_health', 'unknown')}",
            f"CONTAINERS {snapshot.get('containers', 0)}",
        )
    )


def change_play_state(
    org: str,
    competition: str,
    action: str,
    *,
    client: ManagerClient | None = None,
) -> None:
    if action not in {"start", "restart"}:
        raise ValueError(f"Unsupported Play state change: {action}")
    perform_play_action(org, competition, action, client=client, quiet=True)


def cmd_play_up(
    org: str,
    competition: str,
    *,
    gpu: bool | None,
    port: int | None = None,
    proxy_port: int | None = None,
    bind: str | None = None,
    pull: str = "missing",
    wait_timeout: int = PLAY_WAIT_TIMEOUT,
    quiet: bool = False,
    client: ManagerClient | None = None,
    yes: bool = False,
) -> int:
    if port is not None or proxy_port is not None or bind is not None:
        raise RuntimeError(
            "Competition ports are no longer published. Configure the stable manager endpoint with `naij play manager install --bind ... --port ...`."
        )
    snapshot = perform_play_action(
        org,
        competition,
        "play",
        client=client,
        quiet=quiet,
        yes=yes,
        timeout=wait_timeout + 240,
        gpu=gpu,
        pull=pull,
        wait_timeout=wait_timeout,
    )
    if not quiet:
        status = load_play_status(org, competition, client=client or _client(interactive=False))
        print(f"Started {org}/{competition}")
        print(f"Jupyter: {status['jupyter_url']}")
        print(f"Proxy: {status['proxy_url']}")
    return 0


def cmd_play_stop(
    org: str,
    competition: str,
    *,
    quiet: bool = False,
    client: ManagerClient | None = None,
) -> int:
    perform_play_action(org, competition, "stop", client=client, quiet=quiet)
    if not quiet:
        print(f"Stopped {org}/{competition}")
    return 0


def cmd_play_down(
    org: str,
    competition: str,
    *,
    volumes: bool = False,
    force: bool = False,
    quiet: bool = False,
    client: ManagerClient | None = None,
) -> int:
    action = "delete-workspace" if volumes else "delete-container"
    options: dict[str, Any] = {}
    reference = f"{org}/{competition}"
    if volumes:
        if force:
            options["force"] = True
        else:
            if not sys.stdin.isatty():
                raise RuntimeError("Workspace deletion requires --force in non-interactive use")
            confirmation = input(f"Type {reference} to delete its workspace data: ").strip()
            if confirmation != reference:
                print("Aborted.")
                return 1
            options["confirm_ref"] = reference
    perform_play_action(org, competition, action, client=client, quiet=quiet, **options)
    if not quiet:
        print(
            f"Deleted workspace for {reference}"
            if volumes
            else f"Removed containers for {reference}; workspace preserved"
        )
    return 0


def _legacy_port_guidance(args: argparse.Namespace) -> None:
    if any(getattr(args, name, None) is not None for name in ("port", "proxy_port", "bind")):
        raise RuntimeError(
            "--port, --proxy-port, and competition --bind are retired. Use `naij play manager install --bind ADDRESS --port PORT`."
        )


def cmd_manager(args: argparse.Namespace) -> int:
    action = args.manager_action
    if action in {"install", "update"}:
        current = load_manager_config() or {}
        bind = args.bind if args.bind is not None else current.get("bind", DEFAULT_MANAGER_BIND)
        port = args.port if args.port is not None else int(current.get("port", DEFAULT_MANAGER_PORT))
        image = (
            args.image
            if args.image is not None
            else current.get("image")
            or os.environ.get("NAIJ_PLAY_MANAGER_IMAGE")
            or DEFAULT_MANAGER_IMAGE
        )
        tls_cert = args.tls_cert if args.tls_cert is not None else current.get("tls_cert")
        tls_key = args.tls_key if args.tls_key is not None else current.get("tls_key")
        public_url = args.public_url if args.public_url is not None else current.get("public_url")
        info = install_manager(
            bind=bind,
            port=port,
            image=image,
            tls_cert=tls_cert,
            tls_key=tls_key,
            public_url=public_url,
            update=action == "update",
        )
        print(f"Play manager {info.get('manager_version')} is healthy at {public_url or f'http://localhost:{port}'}/nitro/")
        return 0
    if action == "status":
        value = manager_status()
        if not value.get("installed"):
            print("Play manager: not installed")
            return 1
        config = value.get("config") or {}
        print(f"Play manager: {value.get('health', {}).get('status') or value.get('status')}")
        print(f"URL: {config.get('public_url')}/nitro/")
        if value.get("error"):
            print(f"Error: {value['error']}")
            return 1
        return 0
    if action in {"start", "stop", "restart"}:
        manager_compose_action(action)
        print(f"Play manager {action} complete")
        return 0
    if action == "open":
        client = _client(interactive=False)
        url = f"{client.base_url}/nitro/"
        if not webbrowser.open(url):
            raise RuntimeError(f"Open {url} manually")
        print(url)
        return 0
    if action == "uninstall":
        uninstall_manager()
        print("Play manager uninstalled; configuration and all data were preserved")
        return 0
    if action == "purge":
        purge_manager_state(force=args.force)
        print("Manager-private SQLite state was removed; competition workspaces were preserved")
        return 0
    if action == "sync-credentials":
        sync_manager_credentials(required=True)
        print("Nitro login synchronized with the Play manager")
        return 0
    raise RuntimeError(f"Unknown manager action: {action}")


def cmd_play(args: argparse.Namespace) -> int:
    try:
        if args.play_action == "manager":
            return cmd_manager(args)
        org, competition = parse_competition_ref(args.competition)
        _legacy_port_guidance(args)
        client = _client(yes=getattr(args, "yes", False))
        action = args.play_action
        if action == "up":
            action = "play"
        elif action == "down":
            return cmd_play_down(
                org,
                competition,
                volumes=args.volumes,
                force=args.force,
                client=client,
            )
        elif action == "ps":
            print(load_play_ps(org, competition, client=client))
            return 0
        if action == "logs":
            if args.follow:
                for line in client.follow_logs(org, competition):
                    print(line, end="")
            else:
                print(load_play_logs(org, competition, tail=args.tail, client=client))
            return 0
        if action == "status":
            status = load_play_status(org, competition, client=client)
            print(f"Contest: {org}/{competition}")
            print(f"Image: {status.get('image_state', 'unknown')}")
            print(f"Workspace: {status.get('workspace_state', 'unknown')}")
            print(f"Health: {status.get('service_health', 'unknown')}")
            print(f"Jupyter: {status.get('jupyter_url') or '—'}")
            print(f"Dashboard: {status.get('manager_url')}")
            return 0
        if action == "open":
            value = client.open_info(org, competition)
            url = str(value["jupyter_url"])
            if not webbrowser.open(url):
                raise RuntimeError(f"Open {url} manually")
            print(url)
            return 0
        if action == "delete-container":
            return cmd_play_down(org, competition, client=client)
        if action == "delete-workspace":
            return cmd_play_down(
                org,
                competition,
                volumes=True,
                force=args.force,
                client=client,
            )
        if action == "stop":
            return cmd_play_stop(org, competition, client=client)
        options = {
            "pull": getattr(args, "pull", None),
            "gpu": getattr(args, "gpu", None),
            "wait_timeout": getattr(args, "wait_timeout", None),
        }
        result = perform_play_action(
            org,
            competition,
            action,
            client=client,
            yes=getattr(args, "yes", False),
            timeout=getattr(args, "wait_timeout", PLAY_WAIT_TIMEOUT) + 240,
            **options,
        )
        if action in {"play", "recreate"} and getattr(args, "open", False):
            url = client.open_info(org, competition)["jupyter_url"]
            webbrowser.open(str(url))
        print(f"Play {action} complete for {org}/{competition}")
        if result.get("jupyter_url"):
            print(f"Jupyter: {client.base_url}{result['jupyter_url']}")
        return 0
    except (ManagerConnectionError, RuntimeError, ValueError, WireError) as exc:
        print(f"Error: {exc}")
        if isinstance(exc, WireError) and exc.stage:
            print(f"Stage: {exc.stage}")
            for line in exc.logs[-10:]:
                print(line)
        return 1


def normalize_play_argv(argv: list[str]) -> list[str]:
    if not argv or argv[0] not in PLAY_ACTIONS:
        return ["play", *argv]
    return list(argv)


def play_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be an integer") from exc
    if seconds < 1:
        raise argparse.ArgumentTypeError("timeout must be at least 1 second")
    return seconds


def _competition_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("competition", nargs="*", help="[<org>/<comp> | <org> <comp>]")
    parser.add_argument("--yes", action="store_true", help="Install or repair the manager without prompting")


def _runtime_options(parser: argparse.ArgumentParser) -> None:
    gpu = parser.add_mutually_exclusive_group()
    gpu.add_argument("--gpu", dest="gpu", action="store_true", help="Require GPU access")
    gpu.add_argument("--no-gpu", dest="gpu", action="store_false", help="Disable GPU access")
    parser.set_defaults(gpu=None)
    parser.add_argument("--pull", choices=("always", "missing", "never"), default="missing")
    parser.add_argument("--wait-timeout", type=positive_seconds, default=PLAY_WAIT_TIMEOUT)
    parser.add_argument("--open", action="store_true", help="Open Jupyter after the operation")
    parser.add_argument("--port", type=play_port, help=argparse.SUPPRESS)
    parser.add_argument("--proxy-port", type=play_port, help=argparse.SUPPRESS)
    parser.add_argument("--bind", help=argparse.SUPPRESS)


def populate_play_actions(actions: argparse._SubParsersAction) -> None:
    for name, help_text in (
        ("play", "Create or start a competition environment"),
        ("up", "Deprecated alias for play"),
        ("recreate", "Recreate and start competition containers"),
    ):
        command = actions.add_parser(name, help=help_text)
        _competition_argument(command)
        _runtime_options(command)
    pull = actions.add_parser("pull", help="Pull competition images")
    _competition_argument(pull)
    pull.add_argument("--pull", choices=("always", "missing", "never"), default="always")
    for name, help_text in (
        ("start", "Start existing competition containers"),
        ("stop", "Stop containers without removing them"),
        ("restart", "Restart competition containers"),
        ("status", "Show image, workspace, and service state"),
        ("ps", "Show a compact runtime snapshot"),
        ("open", "Open the stable Jupyter URL"),
        ("delete-container", "Delete containers and private network, preserving workspace"),
    ):
        command = actions.add_parser(name, help=help_text)
        _competition_argument(command)
    down = actions.add_parser("down", help="Deprecated alias for delete-container")
    _competition_argument(down)
    down.add_argument("--volumes", action="store_true", help="Also delete workspace data")
    down.add_argument("--force", action="store_true", help="Skip workspace confirmation")
    delete_workspace = actions.add_parser("delete-workspace", help="Permanently delete workspace data")
    _competition_argument(delete_workspace)
    delete_workspace.add_argument("--force", action="store_true", help="Skip typed confirmation")
    logs = actions.add_parser("logs", help="Show redacted competition logs")
    _competition_argument(logs)
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("--tail", type=int, default=80)

    manager = actions.add_parser("manager", help="Install and manage the Play manager")
    manager_actions = manager.add_subparsers(dest="manager_action", required=True, metavar="ACTION")
    for name in ("install", "update"):
        command = manager_actions.add_parser(name, help=f"{name.capitalize()} the manager")
        command.add_argument("--bind")
        command.add_argument("--port", type=play_port)
        command.add_argument("--image")
        command.add_argument("--tls-cert")
        command.add_argument("--tls-key")
        command.add_argument("--public-url")
        command.add_argument("--yes", action="store_true", help="Approve non-interactively")
    for name in ("status", "open", "start", "stop", "restart", "uninstall", "sync-credentials"):
        manager_actions.add_parser(name)
    purge = manager_actions.add_parser("purge", help="Remove manager-private SQLite state")
    purge.add_argument("--force", action="store_true", required=True)


def build_play_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="naij play", add_help=add_help)
    actions = parser.add_subparsers(dest="play_action", required=True, metavar="ACTION")
    populate_play_actions(actions)
    return parser


def add_play_parser(subparsers: argparse._SubParsersAction) -> None:
    play = subparsers.add_parser(
        "play",
        help="Manage Dockerized competition environments through the Play manager",
        description="Manage persistent Nitro competition workspaces at one stable local URL.",
    )
    actions = play.add_subparsers(dest="play_action", required=True, metavar="ACTION")
    populate_play_actions(actions)

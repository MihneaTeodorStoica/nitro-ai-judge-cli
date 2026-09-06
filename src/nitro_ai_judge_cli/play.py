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
    manager_container_exists,
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
    parse_version,
    validate_competition,
)
from .ui import Spinner


PLAY_WAIT_TIMEOUT = 120
MANAGER_SETUP_LABEL = "Installing Play manager (pulling image and waiting for health)\u2026"
MANAGER_ACTION_LABELS = {
    "start": "Starting Play manager\u2026",
    "stop": "Stopping Play manager\u2026",
    "restart": "Restarting Play manager\u2026",
}
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
    "delete-image",
    "delete-workspace",
    "logs",
    "ls",
    "operations",
    "ps",
    "status",
    "cancel",
    "open",
    "manager",
}


class ManagerSetupInterrupted(Exception):
    pass


class OperationWaitInterrupted(Exception):
    pass


def parse_competition_ref(parts: list[str]) -> tuple[str, str]:
    if len(parts) == 1 and parts[0].count("/") == 1:
        org, competition = parts[0].split("/", 1)
    elif len(parts) == 2:
        org, competition = parts
    else:
        raise ValueError("competition must be <org>/<comp> or <org> <comp>")
    return validate_competition(org, competition)


def _setup_manager(**options: Any) -> dict[str, Any]:
    spinner = Spinner(MANAGER_SETUP_LABEL, stream=sys.stdout).start()
    try:
        return install_manager(**options)
    except KeyboardInterrupt as exc:
        raise ManagerSetupInterrupted from exc
    finally:
        spinner.stop()


def _manager_receipt(
    info: dict[str, Any], *, public_url: str | None, port: int
) -> None:
    url = public_url or f"http://localhost:{port}"
    print(f"Play manager {info.get('manager_version')} is healthy at {url}/nitro/")


def _install_or_repair_manager() -> dict[str, Any]:
    config = load_manager_config()
    if not config:
        info = _setup_manager()
        _manager_receipt(info, public_url=None, port=DEFAULT_MANAGER_PORT)
        return info
    port = int(config.get("port") or DEFAULT_MANAGER_PORT)
    public_url = config.get("public_url")
    info = _setup_manager(
        bind=str(config.get("bind") or DEFAULT_MANAGER_BIND),
        port=port,
        image=_update_image(str(config.get("image") or DEFAULT_MANAGER_IMAGE)),
        tls_cert=config.get("tls_cert"),
        tls_key=config.get("tls_key"),
        public_url=public_url,
        update=True,
    )
    _manager_receipt(info, public_url=public_url, port=port)
    return info


def _update_image(image: str) -> str:
    repository, separator, tag = image.rpartition(":")
    default_repository, _, default_tag = DEFAULT_MANAGER_IMAGE.rpartition(":")
    version = parse_version(tag)
    if (
        separator
        and repository == default_repository
        and version != (0, 0, 0)
        and tag == ".".join(map(str, version))
        and version < parse_version(default_tag)
    ):
        return DEFAULT_MANAGER_IMAGE
    return image


def _migrate_manager_if_needed() -> bool:
    config = load_manager_config()
    if not config:
        return False
    image = str(config.get("image") or DEFAULT_MANAGER_IMAGE)
    if _update_image(image) == image:
        return False
    _install_or_repair_manager()
    return True


def _client(*, yes: bool = False, interactive: bool = True) -> ManagerClient:
    _migrate_manager_if_needed()
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
    timeout: float | None = 600,
    detach: bool = False,
    **options: Any,
) -> dict[str, Any]:
    client = client or _client(yes=yes)
    accepted = client.action(org, competition, action, **options)
    operation_id = str(accepted["operation_id"])
    if detach:
        if not quiet:
            print(f"Operation queued: {operation_id}")
            print(f"Status: naij play status {org}/{competition}")
            print(f"Cancel: naij play cancel {org}/{competition}")
        return {"operation_id": operation_id, "detached": True}
    spinner = None
    progress = None if quiet else _progress
    if not quiet and sys.stdout.isatty():
        spinner = Spinner("Operation queued", stream=sys.stdout).start()

        def progress(event: dict[str, Any]) -> None:
            message = str(event.get("message") or "")
            if message:
                spinner.update(message)

    try:
        operation = client.wait_operation(
            operation_id,
            timeout=timeout,
            progress=progress,
        )
    except KeyboardInterrupt as exc:
        raise OperationWaitInterrupted from exc
    finally:
        if spinner is not None:
            spinner.stop()
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


def load_play_ls(*, client: ManagerClient | None = None) -> list[dict[str, Any]]:
    return (client or _client(interactive=False)).competitions()


def format_operations(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No Play operations"
    lines = ["ID  COMPETITION  ACTION  STATUS  UPDATED"]
    for item in items:
        lines.append(
            f"{item.get('id', '?')}  {item.get('competition', '?')}  "
            f"{item.get('action', '?')}  {item.get('status', '?')}  "
            f"{item.get('updated_at', '?')}"
        )
    return "\n".join(lines)


def format_play_ls(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No managed Play environments"
    lines = ["REFERENCE  WORKSPACE  HEALTH  OPERATION"]
    for item in items:
        operation = item.get("operation") if isinstance(item.get("operation"), dict) else {}
        operation_label = str(operation.get("status") or "-") if operation else "-"
        if operation and operation.get("id"):
            operation_label = f"{operation_label}:{operation.get('id')}"
        lines.append(
            f"{item.get('reference', '?')}  "
            f"{item.get('workspace_state', 'unknown')}  "
            f"{item.get('service_health', 'unknown')}  "
            f"{operation_label}"
        )
    return "\n".join(lines)


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
        timeout=None,
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
    detach: bool = False,
) -> int:
    result = perform_play_action(
        org, competition, "stop", client=client, quiet=quiet, detach=detach
    )
    if result.get("detached"):
        return 0
    if not quiet:
        print(f"Stopped {org}/{competition}")
    return 0


def cmd_play_cancel(
    org: str,
    competition: str,
    *,
    client: ManagerClient | None = None,
) -> int:
    client = client or _client(interactive=False)
    reference = f"{org}/{competition}"
    competition_state = next(
        (
            item
            for item in client.competitions()
            if str(item.get("reference") or "") == reference
        ),
        None,
    )
    operation = competition_state.get("operation") if competition_state else None
    operation_id = str(operation.get("id") or "") if isinstance(operation, dict) else ""
    if not operation_id or operation.get("status") not in {"queued", "running"}:
        print(f"No active operation for {reference}.")
        return 1
    cancelled = client.cancel(operation_id)
    status = str(cancelled.get("status") or "")
    if status == "complete":
        print(f"Operation for {reference} already completed before cancellation.")
    elif status == "cancelled":
        print(f"Cancelled active operation for {reference}.")
    else:
        print(f"Operation for {reference} is already {status or 'finished'}.")
    return 0


def cmd_play_down(
    org: str,
    competition: str,
    *,
    volumes: bool = False,
    force: bool = False,
    quiet: bool = False,
    client: ManagerClient | None = None,
    detach: bool = False,
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
    result = perform_play_action(
        org,
        competition,
        action,
        client=client,
        quiet=quiet,
        detach=detach,
        **options,
    )
    if result.get("detached"):
        return 0
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
        image = str(
            args.image
            if args.image is not None
            else current.get("image")
            or os.environ.get("NAIJ_PLAY_MANAGER_IMAGE")
            or DEFAULT_MANAGER_IMAGE
        )
        migrate = args.image is None and _update_image(image) != image
        if args.image is None:
            image = _update_image(image)
        tls_cert = args.tls_cert if args.tls_cert is not None else current.get("tls_cert")
        tls_key = args.tls_key if args.tls_key is not None else current.get("tls_key")
        public_url = args.public_url if args.public_url is not None else current.get("public_url")
        try:
            info = _setup_manager(
                bind=bind,
                port=port,
                image=image,
                tls_cert=tls_cert,
                tls_key=tls_key,
                public_url=public_url,
                update=action == "update" or migrate,
            )
        except ManagerSetupInterrupted:
            print("Manager setup interrupted.")
            return 130
        _manager_receipt(info, public_url=public_url, port=port)
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
        if action != "stop" and _migrate_manager_if_needed():
            return 0
        if action == "start" and (
            not load_manager_config() or not manager_container_exists()
        ):
            _install_or_repair_manager()
            return 0
        spinner = Spinner(MANAGER_ACTION_LABELS[action], stream=sys.stdout).start()
        try:
            manager_compose_action(action)
        finally:
            spinner.stop()
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
        if args.play_action in {"ls", "operations"}:
            client = ManagerClient.from_state()
            verify_manager_info(client.info())
            if args.play_action == "ls":
                print(format_play_ls(load_play_ls(client=client)))
            else:
                print(format_operations(client.operations(limit=args.limit)))
            return 0
        org, competition = parse_competition_ref(args.competition)
        _legacy_port_guidance(args)
        action = args.play_action
        if action == "delete-image" and not args.yes:
            if not sys.stdin.isatty():
                raise RuntimeError(
                    "Image deletion requires --yes in non-interactive use"
                )
            reference = f"{org}/{competition}"
            confirmed = input(
                f"Delete cached competition images for {reference} "
                "(workspace preserved)? [y/N] "
            ).strip().lower()
            if confirmed != "y":
                print("Aborted.")
                return 1
        client = _client(yes=getattr(args, "yes", False))
        if action == "up":
            action = "play"
        elif action == "down":
            return cmd_play_down(
                org,
                competition,
                volumes=args.volumes,
                force=args.force,
                client=client,
                detach=getattr(args, "detach", False),
            )
        elif action == "ps":
            print(load_play_ps(org, competition, client=client))
            return 0
        elif action == "cancel":
            return cmd_play_cancel(org, competition, client=client)
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
            return cmd_play_down(
                org, competition, client=client, detach=getattr(args, "detach", False)
            )
        if action == "delete-workspace":
            return cmd_play_down(
                org,
                competition,
                volumes=True,
                force=args.force,
                client=client,
                detach=getattr(args, "detach", False),
            )
        if action == "stop":
            return cmd_play_stop(
                org, competition, client=client, detach=getattr(args, "detach", False)
            )
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
            timeout=(
                None
                if action in {"pull", "play", "up", "recreate"}
                else getattr(args, "wait_timeout", PLAY_WAIT_TIMEOUT) + 240
            ),
            detach=getattr(args, "detach", False),
            **options,
        )
        if result.get("detached"):
            return 0
        if action in {"play", "recreate"} and getattr(args, "open", False):
            url = client.open_info(org, competition)["jupyter_url"]
            webbrowser.open(str(url))
        print(f"Play {action} complete for {org}/{competition}")
        if result.get("jupyter_url"):
            print(f"Jupyter: {client.base_url}{result['jupyter_url']}")
        return 0
    except ManagerSetupInterrupted:
        print("Manager setup interrupted.")
        return 130
    except OperationWaitInterrupted:
        reference = f"{org}/{competition}"
        print("Operation wait interrupted; the manager operation was not cancelled.")
        print(f"Status: naij play status {reference}")
        print(f"Cancel: naij play cancel {reference}")
        return 130
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130
    except (ManagerConnectionError, RuntimeError, ValueError, WireError) as exc:
        print(f"Error: {exc}")
        if isinstance(exc, WireError) and exc.stage:
            print(f"Stage: {exc.stage}")
            for line in exc.logs[-10:]:
                print(line)
        return 1


def normalize_play_argv(argv: list[str]) -> list[str]:
    if not argv or argv[0] not in (*PLAY_ACTIONS, "-h", "--help"):
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
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Approve prompts and install or repair the manager if needed",
    )


def _runtime_options(parser: argparse.ArgumentParser) -> None:
    gpu = parser.add_mutually_exclusive_group()
    gpu.add_argument("--gpu", dest="gpu", action="store_true", help="Require GPU access")
    gpu.add_argument("--no-gpu", dest="gpu", action="store_false", help="Disable GPU access")
    parser.set_defaults(gpu=None)
    parser.add_argument("--pull", choices=("always", "missing", "never"), default="missing")
    parser.add_argument("--wait-timeout", type=positive_seconds, default=PLAY_WAIT_TIMEOUT)
    completion = parser.add_mutually_exclusive_group()
    completion.add_argument("--open", action="store_true", help="Open Jupyter after the operation")
    completion.add_argument("--detach", action="store_true", help="Queue the operation without waiting")
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
    pull.add_argument("--detach", action="store_true", help="Queue without waiting")
    for name, help_text in (
        ("start", "Start existing competition containers"),
        ("stop", "Stop containers without removing them"),
        ("restart", "Restart competition containers"),
        ("status", "Show image, workspace, and service state"),
        ("cancel", "Cancel the active competition operation"),
        ("ps", "Show a compact runtime snapshot"),
        ("open", "Open the stable Jupyter URL"),
        ("delete-container", "Delete containers and private network, preserving workspace"),
        ("delete-image", "Delete cached competition images, preserving workspace"),
    ):
        command = actions.add_parser(name, help=help_text)
        _competition_argument(command)
        if name in {"start", "stop", "restart", "delete-container", "delete-image"}:
            command.add_argument("--detach", action="store_true", help="Queue without waiting")
    actions.add_parser("ls", help="List all managed competition environments")
    operations = actions.add_parser("operations", help="List recent Play operations")
    operations.add_argument("--limit", type=int, default=20)
    down = actions.add_parser("down", help="Deprecated alias for delete-container")
    _competition_argument(down)
    down.add_argument("--detach", action="store_true", help="Queue without waiting")
    down.add_argument("--volumes", action="store_true", help="Also delete workspace data")
    down.add_argument("--force", action="store_true", help="Skip workspace confirmation")
    delete_workspace = actions.add_parser("delete-workspace", help="Permanently delete workspace data")
    _competition_argument(delete_workspace)
    delete_workspace.add_argument("--detach", action="store_true", help="Queue without waiting")
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
    for name, help_text in (
        ("status", "Show manager status"),
        ("open", "Open the manager dashboard"),
        ("start", "Start the manager"),
        ("stop", "Stop the manager"),
        ("restart", "Restart the manager"),
        ("uninstall", "Uninstall the manager"),
        ("sync-credentials", "Synchronize saved credentials"),
    ):
        manager_actions.add_parser(name, help=help_text)
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

"""Docker CLI backend used only inside the manager container."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import secrets
import tempfile
import time
from typing import Any, Awaitable, Callable, Protocol

import aiohttp

from ..play_protocol import (
    ErrorType,
    ImageState,
    MANAGER_IDENTITY,
    ServiceHealth,
    WireError,
    WorkspaceState,
    competition_key,
)


Progress = Callable[[str, str], Awaitable[None]]
DockerEvent = Callable[[dict[str, Any]], Awaitable[None]]
LABEL_PREFIX = "org.nitro-ai.naij.play"
SHARED_NETWORK = "naij-play"
JUPYTER_CONFIG_DIR = "/etc/naij-jupyter"
PULL_PROGRESS_INTERVAL = 1
# Nitro publishes no designated default; its generic test pair matches a real contest pair.
V3_0_2_FALLBACK_IMAGES = (
    "nitroai/nitro-test-notebook@sha256:9dd89d1c276b550c1c9bf05b7cf60761996a3dec0bc3a013400221416d8ec22e",
    "nitroai/nitro-test-judge-proxy@sha256:46542d51497d689b7d57acf85b143dc52e4022246afedae0d04dc1325358fd24",
)
FALLBACK_IMAGES = (
    "ghcr.io/mihneateodorstoica/nitro-contestant-notebook@sha256:d683327e259d4f1fa9a40203295269b3009a18e8a6d0274e17685efd0e9e3ee0",
    "ghcr.io/mihneateodorstoica/nitro-submission-proxy@sha256:57fb32ae07fd6a231a317796508fa05b3d9902f1b2a2ee1be4937a5f85e39bea",
)
S3_PROXY_CONFIG_ERROR = (
    "Missing required environment variables: S3_URL, S3_BUCKET, S3_ACCESS_KEY_ID"
)
SENSITIVE = re.compile(r"(?i)(authorization|token|password|secret|cookie)([=: ]+)([^\s,;]+)")
SENSITIVE_HEADER = re.compile(
    r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:).*$"
)


def redact(value: str) -> str:
    value = SENSITIVE_HEADER.sub(r"\1 [redacted]", value)
    return SENSITIVE.sub(r"\1\2[redacted]", value)


class Backend(Protocol):
    async def inspect_competition(self, org: str, competition: str) -> dict[str, Any]: ...
    async def images(self, org: str, competition: str) -> dict[str, Any]: ...
    async def perform(
        self,
        org: str,
        competition: str,
        action: str,
        options: dict[str, Any],
        progress: Progress,
        adoption: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    async def logs(self, org: str, competition: str, tail: int) -> str: ...


class DockerBackend:
    def __init__(self, projects_dir: str, manager_image: str) -> None:
        self.projects_dir = projects_dir
        self.manager_image = manager_image
        self.shared_network = os.environ.get("NAIJ_MANAGER_NETWORK", SHARED_NETWORK)
        os.makedirs(projects_dir, mode=0o700, exist_ok=True)

    async def run(
        self,
        command: list[str],
        *,
        check: bool = True,
        input_bytes: bytes | None = None,
        timeout: float = 300,
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input_bytes), timeout=timeout
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise WireError(
                ErrorType.DOCKER_UNAVAILABLE.value,
                f"Docker command timed out after {int(timeout)} seconds",
                stage="applying",
                status=504,
            )
        out = redact(stdout.decode(errors="replace"))
        err = redact(stderr.decode(errors="replace"))
        if check and process.returncode:
            details = (err or out).strip()
            raise WireError(
                ErrorType.OPERATION_FAILED.value,
                details or f"{command[0]} exited with {process.returncode}",
                stage="applying",
                logs=tuple((err or out).splitlines()[-40:]),
                status=500,
            )
        return process.returncode or 0, out, err

    async def watch_events(
        self, callback: DockerEvent, *, since: float | None = None
    ) -> None:
        command = ["docker", "events", "--format", "{{json .}}"]
        if since is not None:
            command.extend(["--since", str(int(since))])
        for resource_type in ("container", "image", "volume", "network"):
            command.extend(["--filter", f"type={resource_type}"])
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    await callback(event)
            returncode = await process.wait()
            assert process.stderr is not None
            error = redact((await process.stderr.read()).decode(errors="replace"))
            if returncode:
                raise WireError(
                    ErrorType.DOCKER_UNAVAILABLE.value,
                    error.strip() or f"docker events exited with {returncode}",
                    stage="watching",
                    status=503,
                )
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()

    @staticmethod
    def names(org: str, competition: str) -> dict[str, str]:
        slug = f"{org}-{competition}"
        return {
            "key": f"{org}/{competition}",
            "project": f"naij-play-{slug}",
            "workspace": f"naij-play-{slug}-workspace",
            "secret": f"naij-play-{slug}-secret",
            "config": f"naij-play-{slug}-jupyter-config",
            "jupyter_alias": f"jupyter-{slug}",
            "proxy_alias": f"proxy-{slug}",
        }

    @staticmethod
    def image_names(org: str, competition: str) -> tuple[str, str]:
        return (
            f"nitroai/{org}-{competition}-notebook:latest",
            f"nitroai/{org}-{competition}-judge-proxy:latest",
        )

    def compose_path(self, org: str, competition: str) -> str:
        return os.path.join(self.projects_dir, f"{org}-{competition}.json")

    def _saved_workspace(self, org: str, competition: str) -> str | None:
        try:
            with open(self.compose_path(org, competition), encoding="utf-8") as stream:
                compose = json.load(stream)
            workspace = compose["services"]["jupyter-server"]["labels"][
                f"{LABEL_PREFIX}.workspace"
            ]
        except (OSError, KeyError, TypeError, ValueError):
            return None
        return workspace if isinstance(workspace, str) and workspace else None

    def _saved_images(self, org: str, competition: str) -> tuple[str, str] | None:
        try:
            with open(self.compose_path(org, competition), encoding="utf-8") as stream:
                services = json.load(stream)["services"]
            images = (
                services["jupyter-server"]["image"],
                services["submission-proxy"]["image"],
            )
        except (OSError, KeyError, TypeError, ValueError):
            return None
        return images if all(isinstance(image, str) and image for image in images) else None

    def compose_command(self, org: str, competition: str, *args: str) -> list[str]:
        names = self.names(org, competition)
        return [
            "docker",
            "compose",
            "--project-name",
            names["project"],
            "--file",
            self.compose_path(org, competition),
            *args,
        ]

    @staticmethod
    def labels(
        org: str, competition: str, role: str, *, workspace: str = ""
    ) -> dict[str, str]:
        labels = {
            f"{LABEL_PREFIX}.owner": MANAGER_IDENTITY,
            f"{LABEL_PREFIX}.identity": f"{org}/{competition}",
            f"{LABEL_PREFIX}.organization": org,
            f"{LABEL_PREFIX}.competition": competition,
            f"{LABEL_PREFIX}.role": role,
            f"{LABEL_PREFIX}.schema": "1",
            f"{LABEL_PREFIX}.api": "1",
        }
        if workspace:
            labels[f"{LABEL_PREFIX}.workspace"] = workspace
        return labels

    @staticmethod
    def _adopted_workspace(adoption: dict[str, Any] | None) -> str:
        if not adoption or not adoption.get("verified"):
            return ""
        manifest = adoption.get("manifest") or {}
        if manifest.get("workspace_kind") != "volume":
            return ""
        return str(manifest.get("workspace_volume") or "")

    async def _image_present(self, image: str) -> bool:
        code, _, _ = await self.run(
            ["docker", "image", "inspect", image], check=False, timeout=30
        )
        return code == 0

    async def images(self, org: str, competition: str) -> dict[str, Any]:
        primary = self.image_names(org, competition)
        ready = await asyncio.gather(
            *(self._image_present(image) for image in (*primary, *FALLBACK_IMAGES))
        )
        saved = self._saved_images(org, competition)
        legacy_fallbacks = (
            f"naij-fallback/{org}-{competition}-notebook:latest",
            f"naij-fallback/{org}-{competition}-judge-proxy:latest",
        )
        values: dict[str, Any] = {}
        for index, role in enumerate(("notebook", "proxy")):
            primary_ready, fallback_ready = ready[index], ready[index + 2]
            use_fallback = not primary_ready and fallback_ready and bool(
                saved
                and saved[index]
                in (FALLBACK_IMAGES[index], legacy_fallbacks[index])
            )
            values[role] = {
                "name": FALLBACK_IMAGES[index] if use_fallback else primary[index],
                "state": (
                    ImageState.READY.value
                    if primary_ready or use_fallback
                    else ImageState.MISSING.value
                ),
                "fallback": use_fallback,
                "fallback_source": FALLBACK_IMAGES[index] if use_fallback else None,
            }
        return values

    async def _preferred_images(self, org: str, competition: str) -> tuple[str, str]:
        images = await self.images(org, competition)
        return images["notebook"]["name"], images["proxy"]["name"]

    async def _volume_details(self, name: str) -> dict[str, Any] | None:
        code, stdout, _ = await self.run(
            ["docker", "volume", "inspect", name], check=False, timeout=30
        )
        if code:
            return None
        try:
            return json.loads(stdout)[0]
        except (ValueError, IndexError, TypeError, json.JSONDecodeError):
            return None

    async def inspect_competition(self, org: str, competition: str) -> dict[str, Any]:
        key = competition_key(org, competition)
        names = self.names(org, competition)
        _, stdout, _ = await self.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={LABEL_PREFIX}.identity={key}",
                "--format",
                "{{json .}}",
            ],
            check=False,
            timeout=30,
        )
        containers = []
        for line in stdout.splitlines():
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        running = bool(containers) and all(
            str(item.get("State", "")).lower() == "running" for item in containers
        )
        stopped = bool(containers) and not running
        workspace_name = self._saved_workspace(org, competition) or names["workspace"]
        workspace = await self._volume_details(workspace_name)
        image_data = await self.images(org, competition)
        health_values = [str(item.get("Status") or "").lower() for item in containers]
        if not containers:
            health = ServiceHealth.UNKNOWN.value
        elif any("unhealthy" in status for status in health_values):
            health = ServiceHealth.UNHEALTHY.value
        elif running:
            health = ServiceHealth.HEALTHY.value
        else:
            health = ServiceHealth.STOPPED.value
        return {
            "organization": org,
            "competition": competition,
            "reference": key,
            "image_state": (
                ImageState.READY.value
                if all(item["state"] == ImageState.READY.value for item in image_data.values())
                else ImageState.MISSING.value
            ),
            "images": image_data,
            "image_fallback": any(item.get("fallback") for item in image_data.values()),
            "workspace_state": (
                WorkspaceState.RUNNING.value
                if running
                else WorkspaceState.STOPPED.value
                if stopped
                else WorkspaceState.READY.value
                if workspace
                else WorkspaceState.MISSING.value
            ),
            "service_health": health,
            "containers": len(containers),
            "workspace": workspace_name if workspace else None,
            "jupyter_url": f"/nitro/competitions/{org}/{competition}/jupyter/",
            "proxy_url": f"/nitro/competitions/{org}/{competition}/proxy/",
            "updated_at": time.time(),
        }

    async def discover(self) -> list[dict[str, Any]]:
        _, stdout, _ = await self.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={LABEL_PREFIX}.owner={MANAGER_IDENTITY}",
                "--format",
                f"{{{{.Label \"{LABEL_PREFIX}.identity\"}}}}",
            ],
            check=False,
            timeout=30,
        )
        snapshots = []
        for key in sorted(set(stdout.splitlines())):
            if "/" not in key:
                continue
            org, competition = key.split("/", 1)
            try:
                snapshots.append(await self.inspect_competition(org, competition))
            except WireError:
                continue
        return snapshots

    async def _create_volume(self, name: str, labels: dict[str, str]) -> None:
        existing = await self._volume_details(name)
        if existing:
            existing_labels = existing.get("Labels") or {}
            if any(existing_labels.get(key) != value for key, value in labels.items()):
                raise WireError(
                    ErrorType.OWNERSHIP_MISMATCH.value,
                    f"Refusing to use volume {name}: ownership labels are incomplete or mismatched",
                    stage="validating",
                    status=409,
                )
            return
        command = ["docker", "volume", "create"]
        for key, value in labels.items():
            command.extend(["--label", f"{key}={value}"])
        command.append(name)
        await self.run(command, timeout=30)

    async def _write_volume_file(
        self,
        volume: str,
        filename: str,
        content: bytes,
        labels: dict[str, str],
        *,
        mode: int = 0o600,
    ) -> None:
        await self._create_volume(volume, labels)
        fixed_script = f"umask 077; cat > /target/{filename}; chmod {mode:o} /target/{filename}"
        await self.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "-v",
                f"{volume}:/target",
                "--entrypoint",
                "/bin/sh",
                self.manager_image,
                "-c",
                fixed_script,
            ],
            input_bytes=content,
            timeout=60,
        )

    async def _gpu_enabled(self, image: str, requested: str) -> bool:
        if requested == "disabled":
            return False
        code, _, stderr = await self.run(
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
            timeout=45,
        )
        if code == 0:
            return True
        if requested == "required":
            reason = (stderr.strip().splitlines() or ["GPU runtime unavailable"])[-1]
            raise WireError(
                ErrorType.OPERATION_FAILED.value,
                f"GPU requested but unavailable: {reason}",
                stage="validating",
                status=409,
            )
        return False

    def _compose(
        self,
        org: str,
        competition: str,
        *,
        gpu: bool,
        pull_policy: str,
        workspace_name: str | None = None,
        images: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        names = self.names(org, competition)
        workspace_name = workspace_name or names["workspace"]
        notebook, proxy = images or self.image_names(org, competition)
        key = names["key"]
        base_path = f"/nitro/competitions/{org}/{competition}/jupyter/"
        proxy_path = f"/nitro/competitions/{org}/{competition}/proxy/"
        proxy_env = {
            name: os.environ[name]
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
            if os.environ.get(name)
        }
        common_labels = self.labels(
            org, competition, "service", workspace=workspace_name
        )
        notebook_service: dict[str, Any] = {
            "image": notebook,
            "pull_policy": pull_policy,
            "command": [base_path],
            "volumes": [
                f"{workspace_name}:/home/jovyan",
                f"{names['config']}:{JUPYTER_CONFIG_DIR}:ro",
            ],
            "environment": {
                "JUPYTER_CONFIG_PATH": JUPYTER_CONFIG_DIR,
                "PROXY_URL": "http://submission-proxy:9000",
                "NITRO_SUBMISSION_PROXY_URL": "http://submission-proxy:9000",
                "PROXY_URL_CLIENT": proxy_path,
                **proxy_env,
            },
            "cap_add": ["NET_ADMIN"],
            "depends_on": ["submission-proxy"],
            "networks": {
                "default": {},
                "nitro": {"aliases": [names["jupyter_alias"]]},
            },
            "labels": {**common_labels, f"{LABEL_PREFIX}.role": "jupyter"},
        }
        if gpu:
            notebook_service["deploy"] = {
                "resources": {
                    "reservations": {
                        "devices": [
                            {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}
                        ]
                    }
                }
            }
        return {
            "name": names["project"],
            "services": {
                "jupyter-server": notebook_service,
                "submission-proxy": {
                    "image": proxy,
                    "pull_policy": pull_policy,
                    "environment": {
                        "JUDGE_BASE_URL": "https://judge.nitro-ai.org/api",
                        "ORGANIZATION_SLUG": org,
                        "COMPETITION_SLUG": competition,
                        "PROXY_PORT": "9000",
                        "JUPYTER_BASE_URL": f"http://jupyter-server:8888{base_path}",
                        "SESSION_WHITELIST_BYPASS_KEY_FILE": "/run/naij-secret/session_whitelist_bypass_key",
                        "PROXY_DISABLE_CACHE": "1",
                        "PROXY_PREJUDGING_TIMEOUT_S": "300",
                        **proxy_env,
                    },
                    "volumes": [
                        f"{names['secret']}:/run/naij-secret:ro"
                    ],
                    "cap_add": ["SYS_ADMIN"],
                    "security_opt": ["seccomp:unconfined", "apparmor:unconfined"],
                    "networks": {
                        "default": {},
                        "nitro": {"aliases": [names["proxy_alias"]]},
                    },
                    "labels": {**common_labels, f"{LABEL_PREFIX}.role": "proxy"},
                },
            },
            "networks": {
                "default": {"labels": self.labels(org, competition, "private-network")},
                "nitro": {"external": True, "name": self.shared_network},
            },
            "volumes": {
                workspace_name: {"external": True, "name": workspace_name},
                names["secret"]: {"external": True, "name": names["secret"]},
                names["config"]: {"external": True, "name": names["config"]},
            },
        }

    def _migrate_jupyter_config_mount(
        self, org: str, competition: str
    ) -> dict[str, Any] | None:
        path = self.compose_path(org, competition)
        with open(path, encoding="utf-8") as stream:
            compose = json.load(stream)
        notebook = compose["services"]["jupyter-server"]
        old_mount = f"{self.names(org, competition)['config']}:/home/jovyan/.jupyter:ro"
        if old_mount not in notebook["volumes"]:
            return None
        original = copy.deepcopy(compose)
        notebook["volumes"][notebook["volumes"].index(old_mount)] = (
            f"{self.names(org, competition)['config']}:{JUPYTER_CONFIG_DIR}:ro"
        )
        notebook["environment"]["JUPYTER_CONFIG_PATH"] = JUPYTER_CONFIG_DIR
        self._atomic_compose(path, compose)
        return original

    async def _prepare_legacy(
        self,
        org: str,
        competition: str,
        adoption: dict[str, Any] | None,
        progress: Progress,
    ) -> dict[str, Any]:
        names = self.names(org, competition)
        workspace_labels = self.labels(
            org, competition, "workspace", workspace=names["workspace"]
        )
        context: dict[str, Any] = {
            "workspace": names["workspace"],
            "container": "",
            "was_running": False,
            "copied_workspace": False,
            "created_workspace": False,
            "created_secret": False,
            "created_config": False,
            "project": "",
        }
        if not adoption or not adoption.get("verified"):
            await self._create_volume(names["workspace"], workspace_labels)
            context["created_workspace"] = True
            return context
        manifest = adoption.get("manifest") or {}
        container = str(manifest.get("container_id") or "")
        if not container:
            await self._create_volume(names["workspace"], workspace_labels)
            return context
        code, stdout, _ = await self.run(
            ["docker", "inspect", container], check=False, timeout=30
        )
        if code:
            workspace = (
                self._saved_workspace(org, competition)
                or self._adopted_workspace(adoption)
                or names["workspace"]
            )
            if workspace == names["workspace"]:
                await self._create_volume(workspace, workspace_labels)
                context["created_workspace"] = True
            elif workspace != self._adopted_workspace(adoption):
                raise WireError(
                    ErrorType.OWNERSHIP_MISMATCH.value,
                    "Saved workspace no longer matches the verified adoption",
                    stage="validating",
                    status=409,
                )
            elif not await self._volume_details(workspace):
                raise WireError(
                    ErrorType.NOT_FOUND.value,
                    f"Verified legacy workspace volume is missing: {workspace}",
                    stage="preparing",
                    status=409,
                )
            context["workspace"] = workspace
            return context
        try:
            inspected = json.loads(stdout)[0]
        except (ValueError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise WireError(
                ErrorType.OWNERSHIP_MISMATCH.value,
                "Verified legacy container could not be inspected",
                stage="validating",
                status=409,
            ) from exc
        labels = inspected.get("Config", {}).get("Labels") or {}
        expected_project = str(manifest.get("project") or "")
        if (
            labels.get("com.docker.compose.project") != expected_project
            or labels.get("com.docker.compose.service") != "jupyter-server"
        ):
            raise WireError(
                ErrorType.OWNERSHIP_MISMATCH.value,
                "Legacy container no longer matches its verified project and service labels",
                stage="validating",
                status=409,
            )
        context.update(
            {
                "container": container,
                "was_running": bool(inspected.get("State", {}).get("Running")),
                "project": expected_project,
            }
        )
        try:
            await progress("preparing", "Stopping verified legacy environment for lazy cutover")
            if context["was_running"]:
                await self.run(["docker", "stop", container], timeout=90)
            legacy_volume = self._adopted_workspace(adoption)
            if legacy_volume:
                if not await self._volume_details(legacy_volume):
                    raise WireError(
                        ErrorType.NOT_FOUND.value,
                        f"Verified legacy workspace volume is missing: {legacy_volume}",
                        stage="preparing",
                        status=409,
                    )
                context["workspace"] = legacy_volume
                return context
            await progress("preparing", "Copying container-layer workspace into a private volume")
            await self._create_volume(names["workspace"], workspace_labels)
            context["created_workspace"] = True
            temporary = f"{names['project']}-workspace-migration"
            try:
                await self.run(
                    [
                        "docker",
                        "create",
                        "--name",
                        temporary,
                        "-v",
                        f"{names['workspace']}:/home/jovyan",
                        "--entrypoint",
                        "/bin/true",
                        self.manager_image,
                    ],
                    timeout=60,
                )
                await self.run(["docker", "start", "--attach", temporary], timeout=60)
                await self.run(
                    [
                        "docker",
                        "cp",
                        "--archive",
                        f"{container}:/home/jovyan/.",
                        f"{temporary}:/home/jovyan",
                    ],
                    timeout=600,
                )
            finally:
                await asyncio.shield(
                    self.run(["docker", "rm", "-f", temporary], check=False, timeout=60)
                )
            context["copied_workspace"] = True
            return context
        except BaseException:
            await asyncio.shield(self._rollback_legacy(org, competition, context))
            raise

    async def _finish_legacy(self, context: dict[str, Any]) -> None:
        container = str(context.get("container") or "")
        if not container:
            return
        await self.run(["docker", "rm", container], timeout=90)
        project = str(context.get("project") or "")
        network = f"{project}_default"
        code, stdout, _ = await self.run(
            ["docker", "network", "inspect", network], check=False, timeout=30
        )
        if code == 0:
            try:
                labels = json.loads(stdout)[0].get("Labels") or {}
            except (ValueError, IndexError, TypeError, json.JSONDecodeError):
                labels = {}
            if labels.get("com.docker.compose.project") == project:
                await self.run(["docker", "network", "rm", network], check=False, timeout=60)

    async def _rollback_legacy(
        self, org: str, competition: str, context: dict[str, Any]
    ) -> None:
        names = self.names(org, competition)
        workspace = str(context["workspace"])
        if context.get("container"):
            await self.run(
                self.compose_command(org, competition, "down", "--remove-orphans"),
                check=False,
                timeout=180,
            )
        if context.get("created_secret"):
            await self._remove_owned_volume(
                names["secret"],
                self.labels(org, competition, "secret", workspace=workspace),
            )
        if context.get("created_config"):
            await self._remove_owned_volume(
                names["config"],
                self.labels(org, competition, "jupyter-config", workspace=workspace),
            )
        if context.get("created_workspace"):
            await self._remove_owned_volume(
                names["workspace"],
                self.labels(
                    org,
                    competition,
                    "workspace",
                    workspace=names["workspace"],
                ),
            )
        if context.get("was_running") and context.get("container"):
            await self.run(
                ["docker", "start", str(context["container"])], check=False, timeout=90
            )

    def _atomic_compose(self, path: str, compose: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".compose-", suffix=".json", dir=os.path.dirname(path)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(compose, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    async def _pull_image(self, image: str, message: str, progress: Progress) -> None:
        task = asyncio.create_task(self.run(["docker", "pull", image], timeout=1800))
        started = time.monotonic()
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=PULL_PROGRESS_INTERVAL
                    )
                    return
                except asyncio.TimeoutError:
                    elapsed = int(time.monotonic() - started)
                    await progress("pulling", f"{message} ({elapsed}s elapsed)")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _image_not_found(error: WireError) -> bool:
        details = "\n".join((error.message, *error.logs)).lower()
        return any(
            marker in details
            for marker in (
                "manifest unknown",
                "pull access denied",
                "repository does not exist",
                "requested access to the resource is denied",
            )
        )

    async def _pull(
        self, org: str, competition: str, policy: str, progress: Progress
    ) -> tuple[str, str]:
        if policy not in {"always", "missing", "never"}:
            raise WireError(
                ErrorType.INVALID_REQUEST.value,
                f"Invalid pull policy: {policy}",
                stage="validating",
                status=400,
            )
        images = self.image_names(org, competition)
        current = await self.images(org, competition)
        saved = self._saved_images(org, competition)
        missing = [
            index
            for index, role in enumerate(("notebook", "proxy"))
            if current[role]["state"] != ImageState.READY.value
        ]
        cached_migrations = {
            index
            for index in missing
            if policy == "never"
            and saved
            and saved[index] == V3_0_2_FALLBACK_IMAGES[index]
            and await self._image_present(FALLBACK_IMAGES[index])
        }
        unresolved = [index for index in missing if index not in cached_migrations]
        if policy == "never" and unresolved:
            raise WireError(
                ErrorType.OPERATION_FAILED.value,
                f"Image is missing and pull policy is never: {images[unresolved[0]]}",
                stage="pulling",
                status=409,
            )
        selected = list(range(2)) if policy == "always" else missing if policy == "missing" else []
        resolved = [
            current[role].get("name") or images[index]
            if current[role]["state"] == ImageState.READY.value
            else images[index]
            for index, role in enumerate(("notebook", "proxy"))
        ]
        for index in cached_migrations:
            resolved[index] = FALLBACK_IMAGES[index]
        for step, index in enumerate(selected, 1):
            image = images[index]
            message = f"Pulling contest image {step}/{len(selected)}: {image}"
            await progress("pulling", message)
            try:
                await self._pull_image(image, message, progress)
                resolved[index] = image
            except WireError as error:
                if not self._image_not_found(error):
                    raise
                fallback = FALLBACK_IMAGES[index]
                await progress(
                    "pulling",
                    f"No contest image is published; using fallback {fallback}",
                )
                fallback_message = f"Pulling fallback image {step}/{len(selected)}: {fallback}"
                await self._pull_image(fallback, fallback_message, progress)
                resolved[index] = fallback
        return resolved[0], resolved[1]

    async def _assert_owned_containers(self, org: str, competition: str) -> None:
        key = f"{org}/{competition}"
        _, stdout, _ = await self.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={self.names(org, competition)['project']}",
                "--format",
                f"{{{{.Label \"{LABEL_PREFIX}.owner\"}}}}|{{{{.Label \"{LABEL_PREFIX}.identity\"}}}}",
            ],
            timeout=30,
        )
        for line in stdout.splitlines():
            if line != f"{MANAGER_IDENTITY}|{key}":
                raise WireError(
                    ErrorType.OWNERSHIP_MISMATCH.value,
                    "Refusing destructive action because a project container is not manager-owned",
                    stage="validating",
                    status=409,
                )

    async def _remove_owned_volume(
        self,
        name: str,
        expected_labels: dict[str, str],
        *,
        adoption: dict[str, Any] | None = None,
    ) -> None:
        details = await self._volume_details(name)
        if not details:
            return
        labels = details.get("Labels") or {}
        adopted_workspace = (
            expected_labels.get(f"{LABEL_PREFIX}.role") == "workspace"
            and self._adopted_workspace(adoption) == name
        )
        if (
            any(labels.get(key) != value for key, value in expected_labels.items())
            and not adopted_workspace
        ):
            raise WireError(
                ErrorType.OWNERSHIP_MISMATCH.value,
                f"Refusing to remove volume {name}: ownership labels are incomplete or mismatched",
                stage="validating",
                status=409,
            )
        await self.run(["docker", "volume", "rm", name], timeout=60)

    async def _wait_services(
        self, org: str, competition: str, timeout: int, progress: Progress
    ) -> None:
        names = self.names(org, competition)
        jupyter = f"http://{names['jupyter_alias']}:8888/nitro/competitions/{org}/{competition}/jupyter/api"
        proxy = f"http://{names['proxy_alias']}:9000/health"
        deadline = time.monotonic() + timeout
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
            while time.monotonic() < deadline:
                ready = []
                for url in (jupyter, proxy):
                    try:
                        async with session.get(url, allow_redirects=False) as response:
                            ready.append(200 <= response.status < 300)
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        ready.append(False)
                if all(ready):
                    return
                _, exited, _ = await self.run(
                    self.compose_command(org, competition, "ps", "--status", "exited", "-q"),
                    check=False,
                    timeout=30,
                )
                if exited.strip():
                    logs = await self.logs(org, competition, 20)
                    raise WireError(
                        ErrorType.OPERATION_FAILED.value,
                        "A Play service exited before becoming ready",
                        stage="verifying",
                        logs=tuple(logs.splitlines()),
                        status=502,
                    )
                await asyncio.sleep(1)
        logs = await self.logs(org, competition, 20)
        raise WireError(
            ErrorType.OPERATION_FAILED.value,
            f"Play services did not become ready within {timeout} seconds",
            stage="verifying",
            logs=tuple(logs.splitlines()),
            status=504,
        )

    async def _wait_services_with_proxy_fallback(
        self, org: str, competition: str, timeout: int, progress: Progress
    ) -> None:
        try:
            await self._wait_services(org, competition, timeout, progress)
            return
        except WireError as error:
            saved = self._saved_images(org, competition)
            if (
                not saved
                or saved[1] != self.image_names(org, competition)[1]
                or not any(S3_PROXY_CONFIG_ERROR in line for line in error.logs)
            ):
                raise

        fallback = FALLBACK_IMAGES[1]
        message = f"Pulling compatible submission proxy fallback: {fallback}"
        await progress(
            "pulling",
            "Contest proxy requires unavailable S3 configuration; using compatible fallback",
        )
        if not await self._image_present(fallback):
            await self._pull_image(fallback, message, progress)

        with open(self.compose_path(org, competition), encoding="utf-8") as stream:
            compose = json.load(stream)
        compose["services"]["submission-proxy"]["image"] = fallback
        self._atomic_compose(self.compose_path(org, competition), compose)
        await progress("applying", "Restarting Play services with compatible fallback")
        await self.run(
            self.compose_command(
                org,
                competition,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "submission-proxy",
            ),
            timeout=180,
        )
        await self.run(
            self.compose_command(
                org,
                competition,
                "up",
                "-d",
                "--no-deps",
                "jupyter-server",
            ),
            timeout=180,
        )
        await self._wait_services(org, competition, timeout, progress)

    async def perform(
        self,
        org: str,
        competition: str,
        action: str,
        options: dict[str, Any],
        progress: Progress,
        adoption: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        names = self.names(org, competition)
        compose_path = self.compose_path(org, competition)
        pull_policy = str(options.get("pull") or "missing")
        resolved_images = None
        if action in {"pull", "play", "recreate"}:
            resolved_images = await self._pull(
                org, competition, pull_policy, progress
            )
        if action == "pull":
            assert resolved_images is not None
            if os.path.isfile(compose_path):
                with open(compose_path, encoding="utf-8") as stream:
                    compose = json.load(stream)
            elif any(
                resolved_images[index] == FALLBACK_IMAGES[index]
                for index in range(2)
            ):
                compose = {
                    "services": {
                        "jupyter-server": {},
                        "submission-proxy": {},
                    }
                }
            else:
                return await self.inspect_competition(org, competition)
            compose["services"]["jupyter-server"]["image"] = resolved_images[0]
            compose["services"]["submission-proxy"]["image"] = resolved_images[1]
            self._atomic_compose(compose_path, compose)
            return await self.inspect_competition(org, competition)
        if action in {"play", "recreate"}:
            await progress("preparing", "Preparing private workspace and configuration")
            legacy_context = await self._prepare_legacy(
                org, competition, adoption, progress
            )
            try:
                legacy_context["created_secret"] = not bool(
                    await self._volume_details(names["secret"])
                )
                await self._write_volume_file(
                    names["secret"],
                    "session_whitelist_bypass_key",
                    (secrets.token_urlsafe(32) + "\n").encode(),
                    self.labels(
                        org,
                        competition,
                        "secret",
                        workspace=str(legacy_context["workspace"]),
                    ),
                )
                base_path = f"/nitro/competitions/{org}/{competition}/jupyter/"
                jupyter_config = (
                "c.ServerApp.base_url = " + repr(base_path) + "\n"
                "c.ServerApp.allow_remote_access = True\n"
                "c.ServerApp.trust_xheaders = True\n"
                "c.ServerApp.allow_origin = ''\n"
                "c.IdentityProvider.token = ''\n"
                ).encode()
                config_labels = self.labels(
                    org,
                    competition,
                    "jupyter-config",
                    workspace=str(legacy_context["workspace"]),
                )
                legacy_context["created_config"] = not bool(
                    await self._volume_details(names["config"])
                )
                await self._write_volume_file(
                    names["config"],
                    "jupyter_server_config.py",
                    jupyter_config,
                    config_labels,
                    mode=0o644,
                )
                await self._write_volume_file(
                    names["config"], "migrated", b"", config_labels, mode=0o644
                )
                gpu_requested = (
                "required" if options.get("gpu") is True else "disabled" if options.get("gpu") is False else "auto"
                )
                assert resolved_images is not None
                images = resolved_images
                gpu = await self._gpu_enabled(images[0], gpu_requested)
                compose = self._compose(
                    org,
                    competition,
                    gpu=gpu,
                    pull_policy="never",
                    workspace_name=str(legacy_context["workspace"]),
                    images=images,
                )
                self._atomic_compose(compose_path, compose)
                await progress("applying", "Starting competition services")
                command = self.compose_command(org, competition, "up", "-d", "--remove-orphans")
                if action == "recreate":
                    command.append("--force-recreate")
                await self.run(command, timeout=float(options.get("wait_timeout") or 120) + 120)
                await progress("verifying", "Checking Jupyter and submission proxy routes")
                await self._wait_services_with_proxy_fallback(
                    org, competition, int(options.get("wait_timeout") or 120), progress
                )
                await self._finish_legacy(legacy_context)
            except BaseException:
                await asyncio.shield(
                    self._rollback_legacy(org, competition, legacy_context)
                )
                raise
            snapshot = await self.inspect_competition(org, competition)
            snapshot["workspace"] = legacy_context["workspace"]
            snapshot["workspace_state"] = WorkspaceState.RUNNING.value
            return snapshot
        if action == "delete-image":
            await progress("validating", "Verifying competition image ownership and usage")
            await self._assert_owned_containers(org, competition)
            _, containers, _ = await self.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"label={LABEL_PREFIX}.identity={org}/{competition}",
                    "--format",
                    "{{.ID}}",
                ],
                timeout=30,
            )
            if containers.strip():
                raise WireError(
                    ErrorType.COMPETITION_BUSY.value,
                    "Remove the competition containers before deleting its images",
                    stage="validating",
                    status=409,
                )
            for image in self.image_names(org, competition):
                if await self._image_present(image):
                    await progress("applying", f"Removing image tag {image}")
                    await self.run(["docker", "image", "rm", image], timeout=60)
            return await self.inspect_competition(org, competition)
        if not os.path.isfile(compose_path) or self._saved_workspace(
            org, competition
        ) is None:
            raise WireError(
                ErrorType.NOT_FOUND.value,
                f"No Play environment exists for {org}/{competition}; run play first",
                stage="validating",
                status=404,
            )
        if action in {"start", "stop", "restart"}:
            await progress("applying", f"{action.capitalize()}ing competition services")
            original_compose = (
                self._migrate_jupyter_config_mount(org, competition)
                if action != "stop"
                else None
            )
            command = (
                self.compose_command(org, competition, "up", "-d", "--force-recreate")
                if original_compose is not None
                else self.compose_command(org, competition, action)
            )
            try:
                await self.run(command, timeout=180)
            except (Exception, asyncio.CancelledError):
                if original_compose is not None:
                    self._atomic_compose(compose_path, original_compose)
                raise
            if action in {"start", "restart"}:
                await progress("verifying", "Checking Jupyter and submission proxy routes")
                await self._wait_services_with_proxy_fallback(
                    org,
                    competition,
                    int(options.get("wait_timeout") or 120),
                    progress,
                )
            return await self.inspect_competition(org, competition)
        if action in {"delete-container", "delete-workspace"}:
            await progress("validating", "Verifying manager ownership labels")
            await self._assert_owned_containers(org, competition)
            workspace_target = (
                self._saved_workspace(org, competition)
                or self._adopted_workspace(adoption)
                or names["workspace"]
            )
            await progress("applying", "Removing competition containers and private network")
            await self.run(
                self.compose_command(org, competition, "down", "--remove-orphans"),
                check=False,
                timeout=180,
            )
            await self._remove_owned_volume(
                names["secret"],
                self.labels(
                    org, competition, "secret", workspace=workspace_target
                ),
            )
            await self._remove_owned_volume(
                names["config"],
                self.labels(
                    org,
                    competition,
                    "jupyter-config",
                    workspace=workspace_target,
                ),
            )
            if action == "delete-workspace":
                await self._remove_owned_volume(
                    workspace_target,
                    self.labels(
                        org,
                        competition,
                        "workspace",
                        workspace=workspace_target,
                    ),
                    adoption=adoption,
                )
                try:
                    os.unlink(compose_path)
                except FileNotFoundError:
                    pass
            snapshot = await self.inspect_competition(org, competition)
            if action == "delete-container" and await self._volume_details(workspace_target):
                snapshot["workspace"] = workspace_target
                snapshot["workspace_state"] = WorkspaceState.READY.value
            return snapshot
        raise WireError(
            ErrorType.INVALID_REQUEST.value,
            f"Unknown Play action: {action}",
            stage="validating",
            status=400,
        )

    async def logs(self, org: str, competition: str, tail: int) -> str:
        path = self.compose_path(org, competition)
        if not os.path.isfile(path):
            return ""
        _, stdout, stderr = await self.run(
            self.compose_command(org, competition, "logs", "--tail", str(max(1, min(tail, 2000)))),
            check=False,
            timeout=60,
        )
        return redact("\n".join(part.strip() for part in (stdout, stderr) if part.strip()))

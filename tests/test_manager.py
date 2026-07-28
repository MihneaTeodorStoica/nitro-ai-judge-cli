from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from aiohttp import WSMsgType, web
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:  # pragma: no cover - host-only installs intentionally omit manager extras
    WSMsgType = web = TestClient = TestServer = None

if web is not None:
    from nitro_ai_judge_cli.manager.app import (
        SESSION_COOKIE,
        _competition_sort_key,
        _docker_event_competitions,
        _new_session,
        _publish_refresh,
        _queue_docker_reconcile,
        _track_operation_task,
        _upsert_snapshot,
        create_app,
    )
    from nitro_ai_judge_cli.manager.backend import (
        FALLBACK_IMAGES,
        PULL_PROGRESS_INTERVAL,
        DockerBackend,
        redact,
    )
    from nitro_ai_judge_cli.manager.store import ManagerStore
    from nitro_ai_judge_cli.play_protocol import ACTION_NAMES, WireError


class FakeBackend:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.block: asyncio.Event | None = None

    @staticmethod
    def names(org: str, competition: str) -> dict[str, str]:
        return {"jupyter_alias": "127.0.0.1", "proxy_alias": "127.0.0.1"}

    @staticmethod
    def image_names(org: str, competition: str) -> tuple[str, str]:
        return (
            f"nitroai/{org}-{competition}-notebook:latest",
            f"nitroai/{org}-{competition}-judge-proxy:latest",
        )

    async def discover(self) -> list[dict]:
        return []

    async def inspect_competition(self, org: str, competition: str) -> dict:
        return {
            "organization": org,
            "competition": competition,
            "reference": f"{org}/{competition}",
            "image_state": "ready",
            "workspace_state": "running",
            "service_health": "healthy",
            "containers": 2,
            "workspace": "workspace",
            "images": await self.images(org, competition),
            "jupyter_url": f"/nitro/competitions/{org}/{competition}/jupyter/",
            "proxy_url": f"/nitro/competitions/{org}/{competition}/proxy/",
        }

    async def images(self, org: str, competition: str) -> dict:
        return {
            "notebook": {"name": "notebook", "state": "ready"},
            "proxy": {"name": "proxy", "state": "ready"},
        }

    async def perform(
        self,
        org: str,
        competition: str,
        action: str,
        options: dict,
        progress,
        adoption=None,
    ) -> dict:
        self.actions.append(action)
        await progress("applying", f"Applying {action}")
        if self.block is not None:
            await self.block.wait()
        return await self.inspect_competition(org, competition)

    async def logs(self, org: str, competition: str, tail: int) -> str:
        return "Authorization: Bearer should-not-leak\nsafe line"


async def next_sse_event(response) -> str:
    event = ""
    while True:
        line = (await response.content.readline()).decode().strip()
        if not line:
            if event:
                return event
            continue
        if line.startswith("event: "):
            event = line.removeprefix("event: ")


@unittest.skipIf(web is None, "aiohttp manager extra is not installed")
class ManagerBackendModelTests(unittest.TestCase):
    def test_delete_image_is_a_protocol_action(self) -> None:
        self.assertIn("delete-image", ACTION_NAMES)

    def test_competition_sort_prioritizes_ongoing_then_featured_missing(self) -> None:
        base = {
            "image_state": "missing",
            "workspace_state": "missing",
            "service_health": "unknown",
            "reference": "org/contest",
        }
        ongoing = {**base, "operation": {"status": "running"}}
        featured = {**base, "featured": True, "competitionStart": 1}
        regular = {**base, "featured": False, "competitionStart": 2}
        self.assertLess(_competition_sort_key(ongoing), _competition_sort_key(featured))
        self.assertLess(_competition_sort_key(featured), _competition_sort_key(regular))
        ready = {**base, "image_state": "ready"}
        self.assertEqual(
            _competition_sort_key({**ready, "featured": True}),
            _competition_sort_key({**ready, "featured": False}),
        )

    def test_compose_keeps_competition_ports_private_and_routes_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = DockerBackend(directory, "manager:test")
            compose = backend._compose(
                "org", "contest", gpu=True, pull_policy="never"
            )
        notebook = compose["services"]["jupyter-server"]
        proxy = compose["services"]["submission-proxy"]
        self.assertNotIn("ports", notebook)
        self.assertNotIn("ports", proxy)
        self.assertEqual(notebook["environment"]["PROXY_URL"], "http://submission-proxy:9000")
        self.assertEqual(
            notebook["environment"]["NITRO_SUBMISSION_PROXY_URL"],
            "http://submission-proxy:9000",
        )
        self.assertEqual(
            notebook["environment"]["PROXY_URL_CLIENT"],
            "/nitro/competitions/org/contest/proxy/",
        )
        self.assertEqual(
            notebook["environment"]["JUPYTER_CONFIG_PATH"],
            "/etc/naij-jupyter",
        )
        self.assertIn(
            "naij-play-org-contest-jupyter-config:/etc/naij-jupyter:ro",
            notebook["volumes"],
        )
        self.assertNotIn(
            "naij-play-org-contest-jupyter-config:/home/jovyan/.jupyter:ro",
            notebook["volumes"],
        )
        self.assertEqual(
            notebook["command"], ["/nitro/competitions/org/contest/jupyter/"]
        )
        self.assertEqual(
            proxy["environment"]["JUPYTER_BASE_URL"],
            "http://jupyter-server:8888/nitro/competitions/org/contest/jupyter/",
        )
        self.assertIn("deploy", notebook)
        self.assertEqual(compose["networks"]["nitro"]["name"], "naij-play")
        self.assertEqual(
            notebook["labels"]["org.nitro-ai.naij.play.identity"], "org/contest"
        )

    def test_redaction_removes_complete_sensitive_headers_and_values(self) -> None:
        value = redact(
            "Authorization: Bearer secret-value\nCookie: session=private\ntoken=abc safe"
        )
        self.assertNotIn("secret-value", value)
        self.assertNotIn("private", value)
        self.assertNotIn("abc", value)
        self.assertIn("safe", value)

    def test_store_marks_running_operations_interrupted_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/manager.db"
            first = ManagerStore(path)
            first.create_operation("operation", "org/contest", "play", {})
            first.event("operation", "applying", "Started")
            second = ManagerStore(path)
            value = second.operation("operation")
            first.close()
            second.close()
        self.assertEqual(value["status"], "interrupted")
        self.assertEqual(value["error"]["stage"], "interrupted")


@unittest.skipIf(web is None, "aiohttp manager extra is not installed")
class ManagerBackendSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.backend = DockerBackend(self.tempdir.name, "manager:test")

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_managed_volumes_require_every_expected_label(self) -> None:
        labels = self.backend.labels("org", "contest", "secret", workspace="workspace")
        self.backend._volume_details = AsyncMock(
            return_value={"Labels": {"org.nitro-ai.naij.play.owner": "naij-play-manager"}}
        )
        with self.assertRaisesRegex(WireError, "incomplete or mismatched"):
            await self.backend._create_volume("secret", labels)

    async def test_adoption_exception_is_only_the_exact_workspace(self) -> None:
        adoption = {
            "verified": True,
            "manifest": {"workspace_kind": "volume", "workspace_volume": "legacy"},
        }
        self.backend._volume_details = AsyncMock(return_value={"Labels": {}})
        self.backend.run = AsyncMock(return_value=(0, "", ""))
        with self.assertRaises(WireError):
            await self.backend._remove_owned_volume(
                "legacy",
                self.backend.labels("org", "contest", "secret", workspace="legacy"),
                adoption=adoption,
            )
        await self.backend._remove_owned_volume(
            "legacy",
            self.backend.labels("org", "contest", "workspace", workspace="legacy"),
            adoption=adoption,
        )
        self.backend.run.assert_awaited_once_with(
            ["docker", "volume", "rm", "legacy"], timeout=60
        )

    async def test_saved_adopted_workspace_survives_legacy_container_removal(self) -> None:
        compose = self.backend._compose(
            "org", "contest", gpu=False, pull_policy="never", workspace_name="legacy"
        )
        self.backend._atomic_compose(
            self.backend.compose_path("org", "contest"), compose
        )
        self.backend.run = AsyncMock(return_value=(1, "", ""))
        self.backend._volume_details = AsyncMock(return_value={"Labels": {}})

        async def progress(_stage: str, _message: str) -> None:
            pass

        context = await self.backend._prepare_legacy(
            "org",
            "contest",
            {
                "verified": True,
                "manifest": {
                    "container_id": "removed",
                    "workspace_kind": "volume",
                    "workspace_volume": "legacy",
                },
            },
            progress,
        )
        self.assertEqual(context["workspace"], "legacy")

    async def test_missing_adopted_volume_restarts_running_legacy_container(self) -> None:
        inspected = json.dumps(
            [{
                "Config": {"Labels": {
                    "com.docker.compose.project": "legacy-project",
                    "com.docker.compose.service": "jupyter-server",
                }},
                "State": {"Running": True},
            }]
        )

        async def run(command, **_options):
            if command[:2] == ["docker", "inspect"]:
                return 0, inspected, ""
            return 0, "", ""

        self.backend.run = AsyncMock(side_effect=run)
        self.backend._volume_details = AsyncMock(return_value=None)
        adoption = {
            "verified": True,
            "manifest": {
                "container_id": "legacy",
                "project": "legacy-project",
                "workspace_kind": "volume",
                "workspace_volume": "missing",
            },
        }
        with self.assertRaisesRegex(WireError, "workspace volume is missing"):
            await self.backend._prepare_legacy(
                "org", "contest", adoption, AsyncMock()
            )
        commands = [call.args[0] for call in self.backend.run.await_args_list]
        self.assertIn(["docker", "stop", "legacy"], commands)
        self.assertIn(["docker", "start", "legacy"], commands)

    async def test_copy_failure_removes_new_workspace_and_restarts_legacy(self) -> None:
        inspected = json.dumps(
            [{
                "Config": {"Labels": {
                    "com.docker.compose.project": "legacy-project",
                    "com.docker.compose.service": "jupyter-server",
                }},
                "State": {"Running": True},
            }]
        )

        async def run(command, **_options):
            if command[:2] == ["docker", "inspect"]:
                return 0, inspected, ""
            if command[:2] == ["docker", "create"]:
                raise RuntimeError("create failed")
            return 0, "", ""

        self.backend.run = AsyncMock(side_effect=run)
        self.backend._create_volume = AsyncMock()
        self.backend._remove_owned_volume = AsyncMock()
        adoption = {
            "verified": True,
            "manifest": {"container_id": "legacy", "project": "legacy-project"},
        }
        with self.assertRaisesRegex(RuntimeError, "create failed"):
            await self.backend._prepare_legacy(
                "org", "contest", adoption, AsyncMock()
            )
        self.backend._remove_owned_volume.assert_awaited_once()
        commands = [call.args[0] for call in self.backend.run.await_args_list]
        self.assertIn(["docker", "start", "legacy"], commands)

    async def test_cancellation_after_legacy_preparation_runs_rollback(self) -> None:
        context = {
            "workspace": "workspace",
            "container": "legacy",
            "was_running": True,
            "created_workspace": False,
            "created_secret": False,
            "created_config": False,
        }
        self.backend._pull = AsyncMock(return_value=("notebook", "proxy"))
        self.backend._prepare_legacy = AsyncMock(return_value=context)
        self.backend._volume_details = AsyncMock(return_value=None)
        self.backend._write_volume_file = AsyncMock(side_effect=asyncio.CancelledError)
        self.backend._rollback_legacy = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await self.backend.perform(
                "org", "contest", "play", {"pull": "never"}, AsyncMock()
            )
        self.backend._rollback_legacy.assert_awaited_once_with(
            "org", "contest", context
        )
    async def test_start_retries_read_only_jupyter_home_mount_migration(self) -> None:
        compose = self.backend._compose(
            "org", "contest", gpu=False, pull_policy="never"
        )
        notebook = compose["services"]["jupyter-server"]
        notebook["volumes"][1] = (
            "naij-play-org-contest-jupyter-config:/home/jovyan/.jupyter:ro"
        )
        notebook["environment"].pop("JUPYTER_CONFIG_PATH")
        self.backend._atomic_compose(
            self.backend.compose_path("org", "contest"), compose
        )
        self.backend.run = AsyncMock(
            side_effect=WireError("operation_failed", "compose failed")
        )
        self.backend._wait_services_with_proxy_fallback = AsyncMock()
        self.backend.inspect_competition = AsyncMock(return_value={})

        with self.assertRaisesRegex(WireError, "compose failed"):
            await self.backend.perform("org", "contest", "start", {}, AsyncMock())
        with open(
            self.backend.compose_path("org", "contest"), encoding="utf-8"
        ) as stream:
            failed_notebook = json.load(stream)["services"]["jupyter-server"]
        self.assertNotIn("JUPYTER_CONFIG_PATH", failed_notebook["environment"])
        self.assertIn(
            "naij-play-org-contest-jupyter-config:/home/jovyan/.jupyter:ro",
            failed_notebook["volumes"],
        )

        self.backend.run = AsyncMock(return_value=(0, "", ""))
        await self.backend.perform("org", "contest", "start", {}, AsyncMock())

        with open(
            self.backend.compose_path("org", "contest"), encoding="utf-8"
        ) as stream:
            notebook = json.load(stream)["services"]["jupyter-server"]
        self.assertEqual(
            notebook["environment"]["JUPYTER_CONFIG_PATH"],
            "/etc/naij-jupyter",
        )
        self.assertIn(
            "naij-play-org-contest-jupyter-config:/etc/naij-jupyter:ro",
            notebook["volumes"],
        )
        self.backend.run.assert_awaited_once_with(
            self.backend.compose_command(
                "org", "contest", "up", "-d", "--force-recreate"
            ),
            timeout=180,
        )

    async def test_ready_is_workspace_only_and_stopped_requires_containers(self) -> None:
        images = {
            "notebook": {"name": "notebook", "state": "ready"},
            "proxy": {"name": "proxy", "state": "ready"},
        }
        self.backend.images = AsyncMock(return_value=images)
        self.backend._volume_details = AsyncMock(return_value={"Labels": {}})
        self.backend.run = AsyncMock(return_value=(0, "", ""))
        ready = await self.backend.inspect_competition("org", "contest")
        self.assertEqual(ready["workspace_state"], "ready")

        self.backend.run.return_value = (
            0,
            json.dumps({"State": "exited", "Status": "Exited (0)"}) + "\n",
            "",
        )
        stopped = await self.backend.inspect_competition("org", "contest")
        self.assertEqual(stopped["workspace_state"], "stopped")

    async def test_wait_services_uses_real_health_routes(self) -> None:
        self.backend.run = AsyncMock(return_value=(0, "", ""))
        jupyter = AsyncMock()
        jupyter.__aenter__.return_value.status = 200
        proxy = AsyncMock()
        proxy.__aenter__.return_value.status = 200
        client = MagicMock()
        client.get.side_effect = [jupyter, proxy]
        session = AsyncMock()
        session.__aenter__.return_value = client
        with patch(
            "nitro_ai_judge_cli.manager.backend.aiohttp.ClientSession",
            return_value=session,
        ):
            await self.backend._wait_services("org", "contest", 1, AsyncMock())
        self.assertTrue(
            client.get.call_args_list[0].args[0].endswith(
                "/nitro/competitions/org/contest/jupyter/api"
            )
        )
        self.assertTrue(client.get.call_args_list[1].args[0].endswith(":9000/health"))

    async def test_wait_services_does_not_accept_redirects_as_ready(self) -> None:
        self.backend.run = AsyncMock(return_value=(0, "", ""))
        responses = []
        for status in (302, 200, 200, 200):
            response = AsyncMock()
            response.__aenter__.return_value.status = status
            responses.append(response)
        client = MagicMock()
        client.get.side_effect = responses
        session = AsyncMock()
        session.__aenter__.return_value = client
        with (
            patch(
                "nitro_ai_judge_cli.manager.backend.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "nitro_ai_judge_cli.manager.backend.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            await self.backend._wait_services("org", "contest", 1, AsyncMock())
        self.assertEqual(client.get.call_count, 4)

    async def test_wait_services_reports_an_exited_proxy_immediately(self) -> None:
        self.backend.run = AsyncMock(return_value=(0, "proxy-container\n", ""))
        self.backend.logs = AsyncMock(return_value="proxy | missing S3 configuration")
        jupyter = AsyncMock()
        jupyter.__aenter__.return_value.status = 200
        proxy = AsyncMock()
        proxy.__aenter__.return_value.status = 503
        client = MagicMock()
        client.get.side_effect = [jupyter, proxy]
        session = AsyncMock()
        session.__aenter__.return_value = client
        with patch(
            "nitro_ai_judge_cli.manager.backend.aiohttp.ClientSession",
            return_value=session,
        ):
            with self.assertRaisesRegex(WireError, "exited before becoming ready") as raised:
                await self.backend._wait_services("org", "contest", 120, AsyncMock())
        self.assertIn("missing S3 configuration", "\n".join(raised.exception.logs))

    async def test_wait_timeout_keeps_proxy_error_logs(self) -> None:
        proxy_error = "submission-proxy | Missing required environment variables: S3_URL"
        self.backend.logs = AsyncMock(
            return_value="\n".join((proxy_error, *(f"jupyter | line {i}" for i in range(20))))
        )
        with self.assertRaises(WireError) as raised:
            await self.backend._wait_services("org", "contest", 0, AsyncMock())
        self.assertIn(proxy_error, raised.exception.as_dict()["logs"])

    async def test_s3_dependent_primary_proxy_retries_with_fallback(self) -> None:
        primary = self.backend.image_names("org", "contest")
        self.backend._atomic_compose(
            self.backend.compose_path("org", "contest"),
            self.backend._compose(
                "org", "contest", gpu=False, pull_policy="never", images=primary
            ),
        )
        error = WireError(
            "operation_failed",
            "A Play service exited before becoming ready",
            logs=(
                "proxy | Missing required environment variables: "
                "S3_URL, S3_BUCKET, S3_ACCESS_KEY_ID",
            ),
        )
        self.backend._wait_services = AsyncMock(side_effect=(error, None))
        self.backend._image_present = AsyncMock(
            side_effect=lambda image: image != FALLBACK_IMAGES[1]
        )
        self.backend._pull_image = AsyncMock()
        self.backend.run = AsyncMock(return_value=(0, "", ""))

        await self.backend._wait_services_with_proxy_fallback(
            "org", "contest", 120, AsyncMock()
        )

        self.backend._pull_image.assert_awaited_once()
        self.assertFalse(
            any(
                call.args[0][:2] == ["docker", "tag"]
                for call in self.backend.run.await_args_list
            )
        )
        self.assertIn(
            self.backend.compose_command(
                "org", "contest", "up", "-d", "--no-deps", "jupyter-server"
            ),
            [call.args[0] for call in self.backend.run.await_args_list],
        )
        self.assertEqual(
            self.backend._saved_images("org", "contest"),
            (primary[0], FALLBACK_IMAGES[1]),
        )
        self.backend._image_present = AsyncMock(
            side_effect=lambda image: image in (primary[0], FALLBACK_IMAGES[1])
        )
        images = await self.backend.images("org", "contest")
        self.assertFalse(images["notebook"]["fallback"])
        self.assertTrue(images["proxy"]["fallback"])
        self.assertEqual(
            await self.backend._preferred_images("org", "contest"),
            (primary[0], FALLBACK_IMAGES[1]),
        )

    async def test_proxy_fallback_does_not_mask_other_startup_errors(self) -> None:
        self.backend._wait_services = AsyncMock(
            side_effect=WireError(
                "operation_failed",
                "A Play service exited before becoming ready",
                logs=("proxy | unrelated failure",),
            )
        )
        self.backend._pull_image = AsyncMock()
        with self.assertRaisesRegex(WireError, "exited before becoming ready"):
            await self.backend._wait_services_with_proxy_fallback(
                "org", "contest", 120, AsyncMock()
            )
        self.backend._pull_image.assert_not_awaited()

    async def test_delete_image_clears_fallback_selection(self) -> None:
        primary = tuple(self.backend.image_names("org", "contest"))
        image_candidates = tuple(dict.fromkeys((*primary, *FALLBACK_IMAGES)))
        self.backend._atomic_compose(
            self.backend.compose_path("org", "contest"),
            self.backend._compose(
                "org", "contest", gpu=False, pull_policy="never", images=FALLBACK_IMAGES
            ),
        )
        removed_images: set[str] = set()

        async def run(command, *_args, **_kwargs):
            if command[:3] == ["docker", "image", "rm"]:
                removed_images.add(command[3])
            return 0, "", ""

        async def image_present(image: str) -> bool:
            return image in image_candidates and image not in removed_images

        self.backend.run = AsyncMock(side_effect=run)
        self.backend._image_present = AsyncMock(side_effect=image_present)

        result = await self.backend.perform(
            "org", "contest", "delete-image", {}, AsyncMock()
        )

        commands = [call.args[0] for call in self.backend.run.await_args_list]
        removed = [
            command for command in commands if command[:3] == ["docker", "image", "rm"]
        ]
        self.assertEqual(
            removed, [["docker", "image", "rm", image] for image in primary]
        )
        self.assertTrue(all("--force" not in command for command in removed))
        self.assertFalse(
            any(command[:3] == ["docker", "compose", "down"] for command in commands)
        )
        self.assertEqual(result["image_state"], "missing")
        self.assertFalse(result.get("image_fallback"))
        self.assertEqual(
            self.backend._saved_images("org", "contest"), primary
        )

    async def test_delete_workspace_preserves_image_metadata(self) -> None:
        workspace_name = self.backend.names("org", "contest")["workspace"]
        self.backend._atomic_compose(
            self.backend.compose_path("org", "contest"),
            self.backend._compose(
                "org",
                "contest",
                gpu=False,
                pull_policy="never",
                images=FALLBACK_IMAGES,
            ),
        )
        removed_volumes: set[str] = set()

        async def run(command, *_args, **_kwargs):
            if command[:3] == ["docker", "volume", "rm"]:
                removed_volumes.add(command[3])
            return 0, "", ""

        async def volume_details(name: str):
            if name == workspace_name and name not in removed_volumes:
                return {
                    "Labels": self.backend.labels(
                        "org", "contest", "workspace", workspace=workspace_name
                    )
                }
            return None

        self.backend._image_present = AsyncMock(
            side_effect=lambda image: image in FALLBACK_IMAGES
        )
        self.backend.run = AsyncMock(side_effect=run)
        self.backend._volume_details = AsyncMock(side_effect=volume_details)

        result = await self.backend.perform(
            "org", "contest", "delete-workspace", {"force": True}, AsyncMock()
        )

        self.assertTrue(
            Path(self.backend.compose_path("org", "contest")).exists(),
        )
        self.assertEqual(result["image_state"], "ready")
        self.assertTrue(result["image_fallback"])
        self.assertEqual(result["workspace_state"], "missing")
        self.assertFalse(
            any(
                call.args[0][:3] == ["docker", "image", "rm"]
                for call in self.backend.run.await_args_list
            )
        )

    async def test_delete_image_refuses_existing_owned_containers(self) -> None:
        self.backend.run = AsyncMock(
            side_effect=(
                (0, "naij-play-manager|org/contest\n", ""),
                (0, "container-id\n", ""),
            )
        )
        self.backend._image_present = AsyncMock()

        with self.assertRaises(WireError) as raised:
            await self.backend.perform(
                "org", "contest", "delete-image", {}, AsyncMock()
            )

        self.assertEqual(raised.exception.type, "competition_busy")
        self.assertEqual(raised.exception.status, 409)
        self.backend._image_present.assert_not_awaited()

    async def test_delete_image_refuses_foreign_project_containers(self) -> None:
        self.backend.run = AsyncMock(return_value=(0, "someone-else|org/contest\n", ""))
        self.backend._image_present = AsyncMock()

        with self.assertRaises(WireError) as raised:
            await self.backend.perform(
                "org", "contest", "delete-image", {}, AsyncMock()
            )

        self.assertEqual(raised.exception.type, "ownership_mismatch")
        self.backend._image_present.assert_not_awaited()

    async def test_missing_contest_images_pull_pinned_fallbacks(self) -> None:
        self.backend.images = AsyncMock(
            return_value={
                "notebook": {"state": "missing"},
                "proxy": {"state": "missing"},
            }
        )

        async def run(command: list[str], **_kwargs):
            if command[:2] == ["docker", "pull"] and command[2].startswith(
                "nitroai/org-contest-"
            ):
                raise WireError(
                    "operation_failed",
                    "pull access denied: repository does not exist",
                    stage="pulling",
                )
            return 0, "", ""

        self.backend.run = AsyncMock(side_effect=run)
        progress = AsyncMock()
        resolved = await self.backend._pull("org", "contest", "missing", progress)

        commands = [call.args[0] for call in self.backend.run.await_args_list]
        for fallback in FALLBACK_IMAGES:
            self.assertIn(["docker", "pull", fallback], commands)
        self.assertFalse(any(command[:2] == ["docker", "tag"] for command in commands))
        self.assertTrue(
            any(
                "using fallback" in call.args[1]
                for call in progress.await_args_list
            )
        )
        self.assertEqual(resolved, FALLBACK_IMAGES)

    async def test_saved_legacy_aliases_select_shared_fallbacks(self) -> None:
        legacy = (
            "naij-fallback/org-contest-notebook:latest",
            "naij-fallback/org-contest-judge-proxy:latest",
        )
        self.backend._atomic_compose(
            self.backend.compose_path("org", "contest"),
            self.backend._compose(
                "org", "contest", gpu=False, pull_policy="never", images=legacy
            ),
        )
        self.backend._image_present = AsyncMock(
            side_effect=lambda image: image in FALLBACK_IMAGES
        )
        images = await self.backend.images("org", "contest")
        self.assertTrue(images["notebook"]["fallback"])
        self.assertEqual(images["notebook"]["fallback_source"], FALLBACK_IMAGES[0])
        self.assertEqual(images["proxy"]["name"], FALLBACK_IMAGES[1])
        compose = self.backend._compose(
            "org", "contest", gpu=False, pull_policy="never", images=FALLBACK_IMAGES
        )
        self.assertEqual(
            compose["services"]["jupyter-server"]["image"], FALLBACK_IMAGES[0]
        )

    async def test_network_pull_errors_do_not_switch_to_fallback(self) -> None:
        self.backend.images = AsyncMock(
            return_value={
                "notebook": {"state": "missing"},
                "proxy": {"state": "ready"},
            }
        )
        self.backend.run = AsyncMock(
            side_effect=WireError(
                "operation_failed", "network is unreachable", stage="pulling"
            )
        )
        with self.assertRaisesRegex(WireError, "network is unreachable"):
            await self.backend._pull("org", "contest", "missing", AsyncMock())
        self.assertEqual(self.backend.run.await_count, 1)

    async def test_pull_reports_elapsed_progress(self) -> None:
        self.assertEqual(PULL_PROGRESS_INTERVAL, 1)

        async def slow_run(_command: list[str], **_kwargs):
            await asyncio.sleep(0.01)
            return 0, "", ""

        self.backend.run = AsyncMock(side_effect=slow_run)
        progress = AsyncMock()
        with patch(
            "nitro_ai_judge_cli.manager.backend.PULL_PROGRESS_INTERVAL", 0.001
        ):
            await self.backend._pull_image("image", "Pulling image", progress)
        self.assertTrue(
            any("elapsed" in call.args[1] for call in progress.await_args_list)
        )

    async def test_cancelling_pull_terminates_docker_process(self) -> None:
        class HangingProcess:
            def __init__(self) -> None:
                self.returncode = None
                self.started = asyncio.Event()
                self.terminated = False
                self.killed = False

            async def communicate(self, _input=None):
                self.started.set()
                await asyncio.Future()

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            async def wait(self) -> int:
                return self.returncode

        process = HangingProcess()
        with patch(
            "nitro_ai_judge_cli.manager.backend.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            pull = asyncio.create_task(
                self.backend._pull_image("image", "Pulling image", AsyncMock())
            )
            await asyncio.wait_for(process.started.wait(), timeout=1)
            pull.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pull
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)


@unittest.skipIf(web is None, "aiohttp manager extra is not installed")
class ManagerRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.backend = FakeBackend()
        self.store = ManagerStore(f"{self.tempdir.name}/manager.db")
        self.app = create_app(
            backend=self.backend,
            store=self.store,
            api_token="cli-secret",
            public_url="http://localhost:51123",
            lan=False,
        )
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.host = {"Host": "localhost:51123"}
        self.auth = {**self.host, "Authorization": "Bearer cli-secret"}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tempdir.cleanup()

    async def test_health_and_info_are_public_but_state_is_authenticated(self) -> None:
        root = await self.client.get("/", headers=self.host, allow_redirects=False)
        self.assertEqual(root.headers["Location"], "/nitro/")
        response = await self.client.get("/nitro/api/v1/info", headers=self.host)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["identity"], "naij-play-manager")
        response = await self.client.get("/nitro/api/v1/health", headers=self.host)
        self.assertEqual(response.status, 200)
        response = await self.client.get("/nitro/api/v1/competitions", headers=self.host)
        self.assertEqual(response.status, 401)

    async def test_sse_is_authenticated_coalesced_and_updates_every_tab(self) -> None:
        denied = await self.client.get(
            "/nitro/api/v1/events", headers=self.host
        )
        self.assertEqual(denied.status, 401)

        first = await self.client.get("/nitro/api/v1/events", headers=self.auth)
        second = await self.client.get("/nitro/api/v1/events", headers=self.auth)
        self.assertEqual(first.headers["Content-Type"], "text/event-stream")
        self.assertEqual(await next_sse_event(first), "sync")
        self.assertEqual(await next_sse_event(second), "sync")
        self.assertEqual(len(self.app["event_subscribers"]), 2)

        for _ in range(10):
            _publish_refresh(self.app)
        self.assertTrue(
            all(queue.qsize() == 1 for queue in self.app["event_subscribers"])
        )
        self.assertEqual(await next_sse_event(first), "refresh")
        self.assertEqual(await next_sse_event(second), "refresh")
        first.close()
        second.close()
        _publish_refresh(self.app)
        for _ in range(20):
            if not self.app["event_subscribers"]:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.app["event_subscribers"])

    async def test_cached_snapshot_skips_docker_and_includes_latest_operation(self) -> None:
        self.store.upsert_competition(
            "org/contest",
            "org",
            "contest",
            {
                "organization": "org",
                "competition": "contest",
                "reference": "org/contest",
                "image_state": "ready",
                "workspace_state": "stopped",
                "service_health": "stopped",
            },
        )
        self.store.create_operation("operation", "org/contest", "start", {})
        self.backend.discover = AsyncMock(side_effect=AssertionError("Docker called"))
        response = await self.client.get(
            "/nitro/api/v1/competitions?cached=true", headers=self.auth
        )
        value = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(value["competitions"][0]["operation"]["id"], "operation")

    async def test_refresh_failure_keeps_cached_rows(self) -> None:
        self.store.upsert_competition(
            "org/cached",
            "org",
            "cached",
            {
                "organization": "org",
                "competition": "cached",
                "reference": "org/cached",
                "image_state": "ready",
                "workspace_state": "stopped",
                "service_health": "stopped",
            },
        )

        async def unavailable(_: web.Request) -> web.Response:
            return web.Response(status=503)

        upstream_app = web.Application()
        upstream_app.router.add_get("/competitions", unavailable)
        upstream = TestServer(upstream_app)
        await upstream.start_server()
        try:
            self.store.put_credentials(
                {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "api_base_url": str(upstream.make_url("/")).rstrip("/"),
                }
            )
            failed = await self.client.get(
                "/nitro/api/v1/competitions?refresh=true", headers=self.auth
            )
        finally:
            await upstream.close()
        self.assertEqual(failed.status, 502)
        cached = await self.client.get(
            "/nitro/api/v1/competitions?cached=true", headers=self.auth
        )
        self.assertEqual(
            (await cached.json())["competitions"][0]["reference"], "org/cached"
        )

    async def test_snapshot_notifications_ignore_volatile_timestamps(self) -> None:
        queue = asyncio.Queue(maxsize=1)
        self.app["event_subscribers"].add(queue)
        snapshot = {
            "organization": "org",
            "competition": "contest",
            "reference": "org/contest",
            "image_state": "ready",
            "workspace_state": "stopped",
            "service_health": "stopped",
            "updated_at": 1,
        }
        self.assertTrue(
            _upsert_snapshot(self.app, "org/contest", "org", "contest", snapshot)
        )
        queue.get_nowait()
        self.assertFalse(
            _upsert_snapshot(
                self.app,
                "org/contest",
                "org",
                "contest",
                {**snapshot, "updated_at": 2},
            )
        )
        self.assertTrue(queue.empty())
        self.assertTrue(
            _upsert_snapshot(
                self.app,
                "org/contest",
                "org",
                "contest",
                {**snapshot, "workspace_state": "running"},
            )
        )
        self.assertEqual(queue.get_nowait(), "refresh")
        self.app["event_subscribers"].discard(queue)

    async def test_docker_events_match_only_owned_or_expected_resources(self) -> None:
        self.store.upsert_competition(
            "org/contest",
            "org",
            "contest",
            {
                "organization": "org",
                "competition": "contest",
                "reference": "org/contest",
                "images": {"notebook": {"name": FALLBACK_IMAGES[0]}},
            },
        )
        owned = {
            "Type": "container",
            "Actor": {
                "Attributes": {
                    "org.nitro-ai.naij.play.owner": "naij-play-manager",
                    "org.nitro-ai.naij.play.identity": "org/contest",
                }
            },
        }
        foreign = {
            "Type": "container",
            "Actor": {"Attributes": {"name": "unrelated"}},
        }
        image = {
            "Type": "image",
            "Actor": {
                "Attributes": {
                    "name": "nitroai/org-contest-notebook:latest"
                }
            },
        }
        fallback = {
            "Type": "image",
            "Actor": {"Attributes": {"name": FALLBACK_IMAGES[0]}},
        }
        self.assertEqual(_docker_event_competitions(self.app, owned), {"org/contest"})
        self.assertEqual(_docker_event_competitions(self.app, image), {"org/contest"})
        self.assertEqual(_docker_event_competitions(self.app, fallback), {"org/contest"})
        self.assertEqual(_docker_event_competitions(self.app, foreign), set())

        self.backend.inspect_competition = AsyncMock(
            return_value={
                "organization": "org",
                "competition": "contest",
                "reference": "org/contest",
                "workspace_state": "running",
            }
        )
        _queue_docker_reconcile(self.app, "org/contest")
        _queue_docker_reconcile(self.app, "org/contest")
        await asyncio.sleep(0.35)
        self.backend.inspect_competition.assert_awaited_once_with("org", "contest")

    async def test_competition_routes_and_all_actions_return_202(self) -> None:
        detail = await self.client.get(
            "/nitro/api/v1/competitions/org/contest", headers=self.auth
        )
        self.assertEqual(detail.status, 200)
        images = await self.client.get(
            "/nitro/api/v1/competitions/org/contest/images", headers=self.auth
        )
        self.assertEqual((await images.json())["notebook"]["state"], "ready")
        for action in (
            "pull",
            "play",
            "start",
            "stop",
            "restart",
            "recreate",
            "delete-image",
            "delete-container",
            "delete-workspace",
        ):
            body = {"force": True} if action == "delete-workspace" else {}
            response = await self.client.post(
                f"/nitro/api/v1/competitions/org/contest/actions/{action}",
                headers=self.auth,
                json=body,
            )
            self.assertEqual(response.status, 202, action)
            operation_id = (await response.json())["operation_id"]
            for _ in range(30):
                operation = await self.client.get(
                    f"/nitro/api/v1/operations/{operation_id}", headers=self.auth
                )
                value = await operation.json()
                if value["status"] == "complete":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(value["status"], "complete", action)

    async def test_operation_update_preserves_remote_display_metadata(self) -> None:
        slug = "nationala-ix-x-2026"
        self.store.upsert_competition(
            f"org/{slug}",
            "org",
            slug,
            {
                "organization": "org",
                "competition": slug,
                "reference": f"org/{slug}",
                "title": "Nationala IX-X 2026",
                "featured": True,
                "competitionStart": 123,
                "obsolete_runtime_field": True,
            },
        )
        self.backend.inspect_competition = AsyncMock(
            return_value={
                "organization": "org",
                "competition": slug,
                "reference": f"org/{slug}",
                "image_state": "ready",
                "image_fallback": False,
                "workspace_state": "running",
                "service_health": "healthy",
            }
        )

        response = await self.client.post(
            f"/nitro/api/v1/competitions/org/{slug}/actions/start",
            headers=self.auth,
            json={},
        )
        operation_id = (await response.json())["operation_id"]
        for _ in range(30):
            operation = await (
                await self.client.get(
                    f"/nitro/api/v1/operations/{operation_id}", headers=self.auth
                )
            ).json()
            if operation["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(operation["status"], "complete")

        competitions = await self.client.get(
            "/nitro/api/v1/competitions", headers=self.auth
        )
        snapshot = (await competitions.json())["competitions"][0]
        self.assertEqual(snapshot["title"], "Nationala IX-X 2026")
        self.assertTrue(snapshot["featured"])
        self.assertEqual(snapshot["competitionStart"], 123)
        self.assertFalse(snapshot["image_fallback"])
        self.assertEqual(snapshot["workspace_state"], "running")
        self.assertNotIn("obsolete_runtime_field", snapshot)

    async def test_idempotent_same_action_and_conflicting_busy_error(self) -> None:
        self.backend.block = asyncio.Event()
        first = await self.client.post(
            "/nitro/api/v1/competitions/org/contest/actions/play",
            headers=self.auth,
            json={"pull": "missing"},
        )
        first_id = (await first.json())["operation_id"]
        same = await self.client.post(
            "/nitro/api/v1/competitions/org/contest/actions/play",
            headers=self.auth,
            json={"pull": "missing"},
        )
        self.assertEqual((await same.json())["operation_id"], first_id)
        conflict = await self.client.post(
            "/nitro/api/v1/competitions/org/contest/actions/stop",
            headers=self.auth,
            json={},
        )
        self.assertEqual(conflict.status, 409)
        self.assertEqual((await conflict.json())["error"]["type"], "competition_busy")
        self.backend.block.set()

    async def test_cancel_waits_for_backend_cleanup_and_workspace_confirmation(self) -> None:
        unconfirmed = await self.client.post(
            "/nitro/api/v1/competitions/org/contest/actions/delete-workspace",
            headers=self.auth,
            json={},
        )
        self.assertEqual(unconfirmed.status, 409)
        backend_started = asyncio.Event()
        backend_stopped = asyncio.Event()

        async def cancellable_perform(
            org, competition, action, options, progress, adoption=None
        ):
            await progress("applying", f"Applying {action}")
            backend_started.set()
            try:
                await asyncio.Future()
            finally:
                backend_stopped.set()

        self.backend.perform = cancellable_perform
        started = await self.client.post(
            "/nitro/api/v1/competitions/org/contest/actions/start",
            headers=self.auth,
            json={},
        )
        operation_id = (await started.json())["operation_id"]
        await asyncio.wait_for(backend_started.wait(), timeout=1)
        unauthorized = await self.client.post(
            f"/nitro/api/v1/operations/{operation_id}/cancel",
            headers=self.host,
            json={},
        )
        self.assertEqual(unauthorized.status, 401)
        self.assertFalse(backend_stopped.is_set())
        cancelled = await self.client.post(
            f"/nitro/api/v1/operations/{operation_id}/cancel",
            headers=self.auth,
            json={},
        )
        self.assertEqual(cancelled.status, 200)
        self.assertEqual((await cancelled.json())["status"], "cancelled")
        self.assertTrue(backend_stopped.is_set())
        self.assertNotIn(operation_id, self.app["operation_tasks"])
        value = await (
            await self.client.get(
                f"/nitro/api/v1/operations/{operation_id}", headers=self.auth
            )
        ).json()
        self.assertEqual(value["status"], "cancelled")
        self.assertEqual(value["events"][-1]["stage"], "cancelled")
        repeated = await self.client.post(
            f"/nitro/api/v1/operations/{operation_id}/cancel",
            headers=self.auth,
            json={},
        )
        self.assertEqual(repeated.status, 200)
        self.assertEqual((await repeated.json())["status"], "cancelled")
        unknown = await self.client.post(
            "/nitro/api/v1/operations/unknown/cancel",
            headers=self.auth,
            json={},
        )
        self.assertEqual(unknown.status, 404)

    async def test_cancel_before_operation_starts_cleans_up_state(self) -> None:
        operation_id = "cancel-before-start"
        self.store.create_operation(operation_id, "org/contest", "pull", {})
        ran = False

        async def operation() -> None:
            nonlocal ran
            ran = True

        task = _track_operation_task(self.app, operation_id, operation())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertFalse(ran)
        self.assertEqual(self.store.operation(operation_id)["status"], "cancelled")
        self.assertNotIn(operation_id, self.app["operation_tasks"])

    async def test_host_session_csrf_origin_and_reserved_slugs(self) -> None:
        rejected = await self.client.get(
            "/nitro/api/v1/info", headers={"Host": "evil.example"}
        )
        self.assertEqual(rejected.status, 400)
        page = await self.client.get("/nitro/", headers=self.host)
        html = await page.text()
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        same_session = await self.client.get("/nitro/", headers=self.host)
        self.assertNotIn("Set-Cookie", same_session.headers)
        missing_csrf = await self.client.put(
            "/nitro/api/v1/credentials",
            headers={**self.host, "Origin": "http://localhost:51123"},
            json={"access_token": "a", "refresh_token": "r"},
        )
        self.assertEqual(missing_csrf.status, 403)
        accepted = await self.client.put(
            "/nitro/api/v1/credentials",
            headers={
                **self.host,
                "Origin": "http://localhost:51123",
                "X-CSRF-Token": csrf,
            },
            json={"access_token": "a", "refresh_token": "r"},
        )
        self.assertEqual(accepted.status, 200)
        reserved = await self.client.get(
            "/nitro/api/v1/competitions/api/contest", headers=self.auth
        )
        self.assertEqual(reserved.status, 400)

    async def test_logs_are_redacted(self) -> None:
        response = await self.client.get(
            "/nitro/api/v1/competitions/org/contest/logs", headers=self.auth
        )
        text = (await response.json())["logs"]
        self.assertNotIn("should-not-leak", text)
        self.assertIn("[redacted]", text)

    async def test_rolling_log_follow_emits_only_new_lines(self) -> None:
        class RollingBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.tails = iter(("A\nB\nC", "B\nC\nD"))

            async def logs(self, org: str, competition: str, tail: int) -> str:
                try:
                    return next(self.tails)
                except StopIteration:
                    raise asyncio.CancelledError

        self.app["backend"] = RollingBackend()
        with patch("nitro_ai_judge_cli.manager.app.asyncio.sleep", return_value=None):
            response = await self.client.get(
                "/nitro/api/v1/competitions/org/contest/logs/follow",
                headers=self.auth,
            )
            events = [json.loads(line)["line"] for line in (await response.text()).splitlines()]
        self.assertEqual(events, ["A", "B", "C", "D"])

    async def test_logged_out_dashboard_prompts_and_accepts_nitro_login(self) -> None:
        received = {}

        async def login(request: web.Request) -> web.Response:
            received.update(await request.post())
            return web.json_response(
                {"accessToken": "access", "refreshToken": "refresh"},
                headers={"x-set-username": "alice"},
            )

        upstream_app = web.Application()
        upstream_app.router.add_post("/api/auth/login", login)
        upstream = TestServer(upstream_app)
        await upstream.start_server()
        try:
            self.app["judge_api_url"] = str(upstream.make_url("/api")).rstrip("/")
            page = await self.client.get("/nitro/", headers=self.host)
            html = await page.text()
            csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
            self.assertIn('id="nitro-login-dialog"', html)
            response = await self.client.post(
                "/nitro/api/v1/login",
                headers={
                    **self.host,
                    "Origin": "http://localhost:51123",
                    "X-CSRF-Token": csrf,
                },
                json={"username": "alice", "password": "private"},
            )
        finally:
            await upstream.close()
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["username"], "alice")
        self.assertEqual(received["username"], "alice")
        self.assertNotEqual(received["password"], "private")
        self.assertEqual(self.store.credentials()["access_token"], "access")

    async def test_competitions_fetch_cache_and_sort_the_full_nitro_list(self) -> None:
        remote = [
            {
                "organizationSlug": "org",
                "competitionSlug": f"filler-{index:03d}",
                "title": f"Filler {index}",
                "competitionStart": 3_000_000 - index,
            }
            for index in range(200)
        ]
        remote.extend(
            {
                "organizationSlug": "org",
                "competitionSlug": competition,
                "title": title,
                "competitionStart": started,
            }
            for competition, title, started in (
                ("ready-error-unhealthy", "Ready error unhealthy", 6_000_000),
                ("ready-error-healthy", "Ready error healthy", 5_000_000),
                ("ready-missing", "Ready missing", 4_000_000),
                ("ready-running-unhealthy", "Ready running unhealthy", 8_000_000),
                ("ready-running-healthy", "Ready running healthy", 7_000_000),
                ("missing-error", "Missing error", 9_000_000),
            )
        )
        queries: list[dict[str, str]] = []
        first_pages = 0
        both_started = asyncio.Event()

        async def list_competitions(request: web.Request) -> web.Response:
            nonlocal first_pages
            queries.append(dict(request.query))
            page = int(request.query["page"])
            size = int(request.query["page_size"])
            if page == 1:
                first_pages += 1
                if first_pages == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=1)
            source = remote[:2] if request.query["featured"] == "true" else remote[2:]
            start = (page - 1) * size
            return web.json_response(source[start : start + size])

        upstream_app = web.Application()
        upstream_app.router.add_get("/competitions", list_competitions)
        upstream = TestServer(upstream_app)
        await upstream.start_server()
        self.store.put_credentials(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "api_base_url": str(upstream.make_url("")).rstrip("/"),
            }
        )
        states = {
            "ready-error-unhealthy": ("ready", "error", "unhealthy"),
            "ready-error-healthy": ("ready", "error", "healthy"),
            "ready-missing": ("ready", "missing", "unknown"),
            "ready-running-unhealthy": ("ready", "running", "unhealthy"),
            "ready-running-healthy": ("ready", "running", "healthy"),
            "ready-stopped": ("ready", "stopped", "stopped"),
            "missing-error": ("missing", "error", "unhealthy"),
        }
        for competition, (image, workspace, health) in states.items():
            reference = f"org/{competition}"
            self.store.upsert_competition(
                reference,
                "org",
                competition,
                {
                    "organization": "org",
                    "competition": competition,
                    "reference": reference,
                    "image_state": image,
                    "workspace_state": workspace,
                    "service_health": health,
                },
            )
        cached_only = await self.client.get(
            "/nitro/api/v1/competitions", headers=self.auth
        )
        self.assertEqual(len((await cached_only.json())["competitions"]), 7)
        self.assertEqual(queries, [])
        try:
            response = await self.client.get(
                "/nitro/api/v1/competitions?refresh=true", headers=self.auth
            )
            value = await response.json()
        finally:
            await upstream.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(
            sorted(queries, key=lambda item: (item["featured"], item["page"])),
            [
                {"page": "1", "page_size": "200", "featured": "false"},
                {"page": "2", "page_size": "200", "featured": "false"},
                {"page": "1", "page_size": "200", "featured": "true"},
            ],
        )
        competitions = value["competitions"]
        self.assertEqual(len(competitions), 207)
        self.assertEqual(
            [item["reference"] for item in competitions[:6]],
            [
                "org/ready-running-unhealthy",
                "org/ready-running-healthy",
                "org/ready-stopped",
                "org/ready-missing",
                "org/ready-error-unhealthy",
                "org/ready-error-healthy",
            ],
        )
        self.assertEqual(competitions[0]["image_state"], "ready")
        self.assertEqual(
            [item["reference"] for item in competitions[6:9]],
            ["org/filler-000", "org/filler-001", "org/filler-002"],
        )
        self.assertEqual(competitions[-1]["reference"], "org/missing-error")
        self.assertEqual(competitions[-1]["competitionStart"], 9_000_000)
        featured = {
            item["reference"] for item in competitions if item.get("featured")
        }
        self.assertEqual(featured, {"org/filler-000", "org/filler-001"})

        self.backend.discover = AsyncMock(
            return_value=[
                {
                    "organization": "org",
                    "competition": "filler-000",
                    "reference": "org/filler-000",
                    "image_state": "missing",
                    "workspace_state": "missing",
                    "service_health": "unknown",
                }
            ]
        )
        cached = await self.client.get(
            "/nitro/api/v1/competitions", headers=self.auth
        )
        cached_value = await cached.json()
        self.assertEqual(cached.status, 200)
        self.assertEqual(
            [item["reference"] for item in cached_value["competitions"]],
            [item["reference"] for item in competitions],
        )
        cached_featured = next(
            item
            for item in cached_value["competitions"]
            if item["reference"] == "org/filler-000"
        )
        self.assertTrue(cached_featured["featured"])
        self.assertEqual(cached_featured["title"], "Filler 0")
        self.assertEqual(cached_featured["competitionStart"], 3_000_000)

    async def test_latest_operation_survives_refresh_with_error_details(self) -> None:
        self.store.upsert_competition(
            "org/contest",
            "org",
            "contest",
            {
                "organization": "org",
                "competition": "contest",
                "reference": "org/contest",
                "image_state": "missing",
                "workspace_state": "missing",
                "service_health": "unknown",
            },
        )
        self.store.create_operation("operation", "org/contest", "pull", {})
        self.store.event("operation", "pulling", "Pulling image (10s elapsed)")
        running = await self.client.get(
            "/nitro/api/v1/competitions", headers=self.auth
        )
        self.assertEqual(
            (await running.json())["competitions"][0]["operation"]["status"],
            "running",
        )

        self.store.fail(
            "operation",
            {
                "type": "operation_failed",
                "stage": "pulling",
                "message": "Image pull failed",
                "logs": ["network unavailable"],
            },
        )
        refreshed = await self.client.get(
            "/nitro/api/v1/competitions", headers=self.auth
        )
        operation = (await refreshed.json())["competitions"][0]["operation"]
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["error"]["logs"], ["network unavailable"])

        self.store.create_operation("operation-2", "org/contest", "pull", {})
        self.store.finish("operation-2", {"image_state": "ready"})
        completed = await self.client.get(
            "/nitro/api/v1/competitions", headers=self.auth
        )
        operation = (await completed.json())["competitions"][0]["operation"]
        self.assertEqual(operation["id"], "operation-2")
        self.assertEqual(operation["status"], "complete")

    async def test_dashboard_paginates_and_searches_the_full_competition_list(self) -> None:
        response = await self.client.get("/nitro/assets/app.js", headers=self.host)
        script = await response.text()
        self.assertIn("const PAGE_SIZE = 25", script)
        self.assertIn("matches.slice(start, start + PAGE_SIZE)", script)
        self.assertIn("keywords.every(keyword => haystack.includes(keyword))", script)
        self.assertIn('refresh ? "?refresh=true" : cached ? "?cached=true" : ""', script)
        self.assertIn('addEventListener("click", () => load({ refresh: true }))', script)
        self.assertIn("competition.operation", script)
        self.assertIn("function effectiveImageState(competition)", script)
        self.assertIn('operation?.stage === "pulling"', script)
        self.assertIn('return "pulling"', script)
        self.assertIn('operation.status === "failed"', script)
        self.assertIn('return "error"', script)
        self.assertIn("return competition.image_state", script)
        self.assertIn("effectiveImageState(item) === imageFilter.dataset.value", script)
        self.assertIn("status(imageStatus, effectiveImageState(competition))", script)
        self.assertIn(
            'actionButton("Remove images", "delete-image", "danger")', script
        )
        self.assertIn('actionButton("Copy link", "copy-link")', script)
        self.assertIn("function jupyterUrl(reference)", script)
        self.assertIn("window.open(\n          jupyterUrl(reference)", script)
        self.assertIn("navigator.clipboard.writeText(jupyterUrl(reference))", script)
        self.assertIn('button.textContent = "Copied"', script)
        self.assertIn('action === "copy-link" ? () => copyJupyterLink', script)
        self.assertIn('setAttribute("aria-live", "polite")', script)
        self.assertIn(
            'const hasContainers = ["running", "stopped"].includes(competition.workspace_state)',
            script,
        )
        self.assertIn(
            'actionButton("Delete container", "delete-container", "danger")', script
        )
        self.assertIn('button.dataset.action === "delete-container"', script)
        self.assertIn(
            'startAction(reference, "delete-container")', script
        )
        self.assertIn(
            'actionButton("Delete volume", "delete-menu", "danger")', script
        )
        self.assertIn("removeImageDialog.showModal()", script)
        self.assertIn('new EventSource("/nitro/api/v1/events")', script)
        self.assertIn('load({ cached: true, silent: true })', script)
        self.assertIn('for (const button of actions.querySelectorAll("button")) button.disabled = busy', script)
        self.assertIn('showAlert(error.message', script)
        self.assertIn('emptyAction.dataset.action === "clear-filters"', script)
        self.assertNotIn("window.confirm", script)
        self.assertIn("setupFilterMenu", script)
        self.assertIn("imageFilter.dataset.value", script)
        self.assertIn('progress.removeAttribute("value")', script)
        self.assertIn('failed ? "Error"', script)
        self.assertIn('event.target.closest(".operation-cancel")', script)
        self.assertIn('/cancel`, { method: "POST" }', script)
        self.assertIn("setInterval(updateElapsedClocks, 100)", script)
        self.assertIn("SUCCESS_DISMISS_MS = 5000", script)
        self.assertIn("dismissedOperations.add(operationId)", script)
        self.assertNotIn("KEPT_OPERATIONS_KEY", script)
        self.assertIn("const deadline = Date.now() + SUCCESS_DISMISS_MS", script)
        self.assertIn("remaining / SUCCESS_DISMISS_MS * 100", script)
        self.assertIn("requestAnimationFrame(tick)", script)
        self.assertIn("cancelAnimationFrame(frame)", script)
        self.assertIn('document.querySelector("#clear-operations")', script)
        lifecycle = script.split("function showOperation", 1)[1].split(
            'rows.addEventListener("click"', 1
        )[0]
        self.assertNotIn('addEventListener("pointerenter"', lifecycle)
        self.assertEqual(
            lifecycle.count("state.operationTimers.get(operationId) !== timer"), 2
        )
        self.assertIn("previousImageState !== effectiveImageState(competition)", lifecycle)
        self.assertIn("if (imageStateChanged) render()", lifecycle)
        self.assertIn("else updateOperation(operationRow, latest)", lifecycle)
        clear_handler = script.split(
            'document.querySelector("#clear-operations")', 1
        )[1].split('document.querySelector("#theme-toggle")', 1)[0]
        for terminal_status in ("complete", "failed", "cancelled", "interrupted"):
            self.assertIn(f'"{terminal_status}"', clear_handler)
        self.assertNotIn('"queued"', clear_handler)
        self.assertNotIn('"running"', clear_handler)
        page = await self.client.get("/nitro/", headers=self.auth)
        page_text = await page.text()
        self.assertIn('id="remove-image-dialog"', page_text)
        self.assertIn('id="image-filter-label">Image state', page_text)
        self.assertIn('id="workspace-filter-label">Workspace state', page_text)
        self.assertIn('class="filter-select" id="image-filter"', page_text)
        self.assertIn('class="filter-select" id="workspace-filter"', page_text)
        self.assertIn('class="filter-value" id="image-filter-value">Any', page_text)
        self.assertIn('class="filter-value" id="workspace-filter-value">Any', page_text)
        self.assertIn('class="operation-cancel"', page_text)
        self.assertNotIn("<select", page_text)
        styles = await self.client.get("/nitro/assets/app.css", headers=self.host)
        style_text = await styles.text()
        self.assertIn(".filter-options", style_text)
        self.assertIn("height: 43px; min-height: 43px", style_text)
        self.assertIn(".offline-panel { width: min(560px, 100%); }", style_text)
        self.assertNotIn("scrollbar-gutter", style_text)
        self.assertIn(
            '.operation-row[data-status="failed"] .operation', style_text
        )
        self.assertIn("width: 40px; height: 40px;", style_text)
        self.assertIn("width: 44px; height: 44px; margin-bottom: 30px", style_text)
        self.assertIn(
            "background: transparent; border: 0; font-size: 1.2rem", style_text
        )
        self.assertIn(
            "background: color-mix(in srgb, var(--ink) 7%, transparent)",
            style_text,
        )
        self.assertIn(".result-summary", style_text)
        self.assertIn(".judge-preview:hover .judge-preview-card", style_text)
        self.assertIn(".judge-preview:focus .judge-preview-card", style_text)
        page = await self.client.get("/nitro/", headers=self.host)
        html = await page.text()
        self.assertEqual(html.count("data-pagination"), 2)
        self.assertEqual(html.count('data-page-action="previous"'), 2)
        self.assertEqual(html.count('data-page-action="next"'), 2)
        self.assertEqual(html.count("data-page-status"), 2)
        self.assertNotIn('id="previous-page"', html)
        self.assertNotIn('id="next-page"', html)
        self.assertIn('class="operation-dismiss"', html)
        self.assertIn('title="Dismiss" hidden>&times;</button>', html)
        self.assertIn('id="clear-operations"', html)
        self.assertIn('rel="icon" type="image/svg+xml" href="/nitro/assets/logo.svg"', html)
        self.assertIn('class="brand-logo"', html)
        self.assertIn('id="disconnect-nitro"', html)
        self.assertIn('id="browser-logout"', html)
        self.assertIn('id="live-alert"', html)
        self.assertIn('id="remove-image-confirm">Remove images', html)
        self.assertIn("<h2>Delete volume?</h2>", html)
        self.assertIn('class="danger-button">Delete volume</button>', html)
        self.assertIn('id="remove-container-dialog"', html)
        self.assertIn('id="remove-container-confirm">Yes, delete container', html)
        self.assertIn('autofocus>Cancel', html)
        self.assertNotIn('class="route-mark"', html)
        self.assertIn("Made with &lt;3 by Mihnea-Teodor Stoica, for the", html)
        self.assertIn('class="judge-preview" tabindex="0"', html)
        self.assertIn('src="/nitro/assets/nitro-duck.png"', html)
        startup = script.rsplit("setInterval(updateElapsedClocks, 100)", 1)[1]
        self.assertIn(
            "load().then(() => load({ refresh: true })).finally(connectEvents)",
            startup,
        )

    async def test_dashboard_ready_action_is_play_not_start(self) -> None:
        response = await self.client.get("/nitro/assets/app.js", headers=self.host)
        script = await response.text()
        self.assertIn('"_blank",\n          "noopener"', script)
        ready = script.split('workspace_state === "ready"', 1)[1].split("} else", 1)[0]
        self.assertIn('actionButton("Play", "play"', ready)
        self.assertNotIn('actionButton("Start", "start"', ready)

    async def test_dashboard_caches_stopped_manager_instructions(self) -> None:
        script = await (
            await self.client.get("/nitro/assets/app.js", headers=self.host)
        ).text()
        self.assertIn('serviceWorker.register("/nitro/assets/sw.js"', script)

        worker = await self.client.get("/nitro/assets/sw.js", headers=self.host)
        worker_script = await worker.text()
        self.assertEqual(worker.headers["Service-Worker-Allowed"], "/nitro/")
        self.assertEqual(worker.headers["Cache-Control"], "no-cache")
        self.assertIn('CACHE = "naij-play-manager-offline-v4"', worker_script)
        self.assertIn('const OFFLINE = "/nitro/assets/offline.html"', worker_script)
        self.assertLess(
            worker_script.index("fetch(event.request)"),
            worker_script.index("caches.match(event.request,"),
        )
        self.assertIn(
            "caches.match(event.request, { ignoreSearch: true })", worker_script
        )

        offline = await self.client.get(
            "/nitro/assets/offline.html", headers=self.host
        )
        page = await offline.text()
        self.assertIn("Play manager is stopped", page)
        self.assertIn("naij play manager start", page)
        self.assertIn("Copy start command", page)
        self.assertIn('href="/nitro/">Retry connection', page)
        self.assertIn('src="/nitro/assets/logo.svg"', page)
        self.assertIn("machine hosting the Play manager", page)

        logo = await self.client.get("/nitro/assets/logo.svg", headers=self.host)
        self.assertEqual(logo.content_type, "image/svg+xml")
        self.assertNotIn("<!DOCTYPE", await logo.text())
        duck = await self.client.get("/nitro/assets/nitro-duck.png", headers=self.host)
        self.assertEqual(duck.content_type, "image/png")
        self.assertTrue((await duck.read()).startswith(b"\x89PNG\r\n\x1a\n"))
        inter = await self.client.get("/nitro/assets/inter-latin.woff2", headers=self.host)
        self.assertEqual(inter.content_type, "font/woff2")

    async def test_credentials_and_legacy_manifest_are_private_state(self) -> None:
        credentials = await self.client.put(
            "/nitro/api/v1/credentials",
            headers=self.auth,
            json={"access_token": "a", "refresh_token": "r", "username": "u"},
        )
        self.assertEqual(credentials.status, 200)
        adoption = await self.client.post(
            "/nitro/api/v1/legacy-adoptions",
            headers=self.auth,
            json={
                "manifests": [
                    {
                        "organization": "org",
                        "competition": "contest",
                        "reference": "org/contest",
                        "project": "nitro-org-contest",
                        "container_id": "abc",
                        "verified": True,
                        "host_path": "/must/not/be-stored",
                    }
                ]
            },
        )
        self.assertEqual((await adoption.json())["adopted"], 1)
        stored = self.store.adoption("org/contest")["manifest"]
        self.assertNotIn("host_path", stored)
        deleted = await self.client.delete("/nitro/api/v1/credentials", headers=self.auth)
        self.assertEqual(deleted.status, 200)
        self.assertIsNone(self.store.credentials())

    async def test_workspace_deletion_clears_completed_adoption(self) -> None:
        self.store.put_adoption(
            "org/contest",
            {
                "workspace_kind": "volume",
                "workspace_volume": "legacy-workspace",
            },
            True,
        )
        response = await self.client.post(
            "/nitro/api/v1/competitions/org/contest/actions/delete-workspace",
            headers=self.auth,
            json={"force": True},
        )
        operation_id = (await response.json())["operation_id"]
        for _ in range(30):
            value = await (
                await self.client.get(
                    f"/nitro/api/v1/operations/{operation_id}", headers=self.auth
                )
            ).json()
            if value["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(value["status"], "complete")
        self.assertIsNone(self.store.adoption("org/contest"))

    async def test_lan_dashboard_requires_rate_limited_token_login(self) -> None:
        lan_store = ManagerStore(f"{self.tempdir.name}/lan.db")
        lan_app = create_app(
            backend=FakeBackend(),
            store=lan_store,
            api_token="api",
            dashboard_token="dashboard-secret",
            public_url="https://play.example:51123",
            lan=True,
        )
        lan = TestClient(
            TestServer(lan_app)
        )
        await lan.start_server()
        try:
            headers = {"Host": "play.example:51123"}
            competition = await lan.post(
                "/nitro/competitions/org/contest/jupyter/api/sessions",
                headers={**headers, "Origin": "https://play.example:51123"},
            )
            self.assertEqual(competition.status, 401)
            page = await lan.get("/nitro/", headers=headers)
            self.assertIn("Protected network dashboard", await page.text())
            denied = await lan.post(
                "/nitro/login",
                headers=headers,
                data={"token": "wrong"},
                allow_redirects=False,
            )
            self.assertEqual(denied.status, 401)
            self.assertEqual(denied.content_type, "text/html")
            self.assertIn("token is invalid", await denied.text())
            accepted = await lan.post(
                "/nitro/login",
                headers=headers,
                data={"token": "dashboard-secret"},
                allow_redirects=False,
            )
            self.assertEqual(accepted.status, 302)
            self.assertIn("Secure", accepted.headers.get("Set-Cookie", ""))

            first_id, first_session = _new_session(lan_app)
            second_id, _ = _new_session(lan_app)
            logout = await lan.post(
                "/nitro/api/v1/logout",
                headers={
                    **headers,
                    "Origin": "https://play.example:51123",
                    "X-CSRF-Token": first_session["csrf"],
                    "Cookie": f"naij_manager_session={first_id}",
                },
            )
            self.assertEqual(logout.status, 200)
            self.assertNotIn(first_id, lan_app["sessions"])
            self.assertIn(second_id, lan_app["sessions"])
            self.assertIn("Max-Age=0", logout.headers.get("Set-Cookie", ""))

            expired_id, expired = _new_session(lan_app)
            expired["expires"] = 0
            expired_page = await lan.get(
                "/nitro/",
                headers={**headers, "Cookie": f"naij_manager_session={expired_id}"},
            )
            self.assertEqual(expired_page.content_type, "text/html")
            self.assertIn("session expired", await expired_page.text())
        finally:
            await lan.close()


@unittest.skipIf(web is None, "aiohttp manager extra is not installed")
class ManagerProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        async def echo(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "path": request.path,
                    "query": request.query_string,
                    "authorization": request.headers.get("Authorization"),
                    "csrf": request.headers.get("X-CSRF-Token"),
                    "cookie": request.headers.get("Cookie"),
                }
            )

        async def websocket(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse(
                protocols=("v1.kernel.websocket.jupyter.org",)
            )
            await socket.prepare(request)
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    await socket.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await socket.send_bytes(message.data)
            return socket

        async def websocket_headers(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json(
                {
                    "authorization": request.headers.get("Authorization"),
                    "csrf": request.headers.get("X-CSRF-Token"),
                    "cookie": request.headers.get("Cookie"),
                }
            )
            return socket

        async def relative_redirect(_: web.Request) -> web.Response:
            raise web.HTTPFound("nitro/competitions/org/contest/jupyter")

        async def set_cookie(_: web.Request) -> web.Response:
            response = web.Response()
            response.set_cookie("upstream_stale", "stale", path="/")
            return response

        self.upstream = web.Application()
        self.upstream.router.add_get("/nitro/competitions/org/contest/jupyter/api/kernels/ws", websocket)
        self.upstream.router.add_get("/nitro/competitions/org/contest/jupyter/api/headers/ws", websocket_headers)
        self.upstream.router.add_get(
            "/nitro/competitions/org/contest/jupyter/relative-redirect",
            relative_redirect,
        )
        self.upstream.router.add_get(
            "/nitro/competitions/org/contest/jupyter/set-cookie", set_cookie
        )
        self.upstream.router.add_route("*", "/{tail:.*}", echo)
        self.runner = web.AppRunner(self.upstream)
        await self.runner.setup()
        self.sites = []
        try:
            for port in (8888, 9000):
                site = web.TCPSite(self.runner, "127.0.0.1", port)
                await site.start()
                self.sites.append(site)
        except OSError as exc:
            await self.runner.cleanup()
            self.skipTest(f"fixed internal test ports unavailable: {exc}")

        self.tempdir = tempfile.TemporaryDirectory()
        app = create_app(
            backend=FakeBackend(),
            store=ManagerStore(f"{self.tempdir.name}/manager.db"),
            api_token="secret",
            public_url="http://localhost:51123",
            lan=False,
        )
        self.app = app
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {"Host": "localhost:51123", "Authorization": "Bearer secret"}

    async def asyncTearDown(self) -> None:
        if hasattr(self, "client"):
            await self.client.close()
        if hasattr(self, "runner"):
            await self.runner.cleanup()
        if hasattr(self, "tempdir"):
            self.tempdir.cleanup()

    async def test_jupyter_keeps_prefix_and_proxy_strips_only_proxy_prefix(self) -> None:
        jupyter = await self.client.get(
            "/nitro/competitions/org/contest/jupyter/api/status?x=1",
            headers=self.headers,
        )
        self.assertEqual(
            (await jupyter.json())["path"],
            "/nitro/competitions/org/contest/jupyter/api/status",
        )
        self.assertIn(
            "'unsafe-eval'", jupyter.headers["Content-Security-Policy"]
        )
        proxy = await self.client.get(
            "/nitro/competitions/org/contest/proxy/submissions?id=2",
            headers=self.headers,
        )
        value = await proxy.json()
        self.assertEqual(value["path"], "/submissions")
        self.assertEqual(value["query"], "id=2")
        self.assertNotIn("'unsafe-eval'", proxy.headers["Content-Security-Policy"])

    async def test_relative_full_prefix_redirect_is_not_doubled(self) -> None:
        response = await self.client.get(
            "/nitro/competitions/org/contest/jupyter/relative-redirect",
            headers=self.headers,
            allow_redirects=False,
        )
        self.assertEqual(
            response.headers["Location"],
            "/nitro/competitions/org/contest/jupyter/",
        )

    async def test_upstream_cookies_are_left_to_the_browser(self) -> None:
        await self.client.get(
            "/nitro/competitions/org/contest/jupyter/set-cookie",
            headers=self.headers,
        )
        self.client.session.cookie_jar.clear()
        response = await self.client.get(
            "/nitro/competitions/org/contest/jupyter/headers",
            headers={**self.headers, "Cookie": "competition=fresh"},
        )
        self.assertEqual((await response.json())["cookie"], "competition=fresh")

    async def test_kernel_websocket_is_bidirectional(self) -> None:
        socket = await self.client.ws_connect(
            "/nitro/competitions/org/contest/jupyter/api/kernels/ws",
            headers=self.headers,
        )
        await socket.send_str("kernel-message")
        message = await socket.receive(timeout=2)
        self.assertEqual(message.data, "kernel-message")
        await socket.close()

    async def test_kernel_websocket_preserves_protocol_and_binary_frames(self) -> None:
        protocol = "v1.kernel.websocket.jupyter.org"
        socket = await self.client.ws_connect(
            "/nitro/competitions/org/contest/jupyter/api/kernels/ws",
            headers=self.headers,
            protocols=(protocol,),
        )
        self.assertEqual(socket.protocol, protocol)
        await socket.send_bytes(b"\x01kernel-message")
        message = await socket.receive(timeout=2)
        self.assertEqual(message.type, WSMsgType.BINARY)
        self.assertEqual(message.data, b"\x01kernel-message")
        await socket.close()

    async def test_manager_credentials_are_stripped_for_http_and_websocket(self) -> None:
        headers = {
            **self.headers,
            "X-CSRF-Token": "manager-csrf",
            "Cookie": "naij_manager_session=manager; competition=safe",
        }
        response = await self.client.get(
            "/nitro/competitions/org/contest/proxy/headers",
            headers=headers,
        )
        value = await response.json()
        self.assertIsNone(value["authorization"])
        self.assertIsNone(value["csrf"])
        self.assertEqual(value["cookie"], "competition=safe")

        socket = await self.client.ws_connect(
            "/nitro/competitions/org/contest/jupyter/api/headers/ws",
            headers=headers,
        )
        value = (await socket.receive_json(timeout=2))
        self.assertIsNone(value["authorization"])
        self.assertIsNone(value["csrf"])
        self.assertEqual(value["cookie"], "competition=safe")
        await socket.close()

    async def test_local_jupyter_post_survives_manager_session_restart(self) -> None:
        stale, _ = _new_session(self.app)
        self.app["sessions"].clear()
        response = await self.client.post(
            "/nitro/competitions/org/contest/jupyter/api/sessions",
            headers={
                "Host": "localhost:51123",
                "Origin": "http://localhost:51123",
                "Cookie": f"{SESSION_COOKIE}={stale}",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertIsNone((await response.json())["cookie"])

        denied = await self.client.post(
            "/nitro/competitions/org/contest/jupyter/api/sessions",
            headers={
                "Host": "localhost:51123",
                "Origin": "https://attacker.example",
                "Cookie": f"{SESSION_COOKIE}={stale}",
            },
        )
        self.assertEqual(denied.status, 403)


if __name__ == "__main__":
    unittest.main()

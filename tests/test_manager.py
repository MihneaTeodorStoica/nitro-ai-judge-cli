from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from aiohttp import WSMsgType, web
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:  # pragma: no cover - host-only installs intentionally omit manager extras
    WSMsgType = web = TestClient = TestServer = None

if web is not None:
    from nitro_ai_judge_cli.manager.app import create_app
    from nitro_ai_judge_cli.manager.backend import DockerBackend, redact
    from nitro_ai_judge_cli.manager.store import ManagerStore
    from nitro_ai_judge_cli.play_protocol import WireError


class FakeBackend:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.block: asyncio.Event | None = None

    @staticmethod
    def names(org: str, competition: str) -> dict[str, str]:
        return {"jupyter_alias": "127.0.0.1", "proxy_alias": "127.0.0.1"}

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


@unittest.skipIf(web is None, "aiohttp manager extra is not installed")
class ManagerBackendModelTests(unittest.TestCase):
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
            notebook["environment"]["PROXY_URL_CLIENT"],
            "/nitro/competitions/org/contest/proxy/",
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

    async def test_wait_services_uses_local_proxy_health_route(self) -> None:
        jupyter = AsyncMock()
        jupyter.__aenter__.return_value.status = 302
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
        self.assertTrue(client.get.call_args_list[1].args[0].endswith(":9000/health"))


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

    async def test_cancel_and_workspace_confirmation(self) -> None:
        unconfirmed = await self.client.post(
            "/nitro/api/v1/competitions/org/contest/actions/delete-workspace",
            headers=self.auth,
            json={},
        )
        self.assertEqual(unconfirmed.status, 409)
        self.backend.block = asyncio.Event()
        started = await self.client.post(
            "/nitro/api/v1/competitions/org/contest/actions/start",
            headers=self.auth,
            json={},
        )
        operation_id = (await started.json())["operation_id"]
        cancelled = await self.client.post(
            f"/nitro/api/v1/operations/{operation_id}/cancel",
            headers=self.auth,
            json={},
        )
        self.assertEqual(cancelled.status, 202)
        await asyncio.sleep(0.02)
        value = await (
            await self.client.get(
                f"/nitro/api/v1/operations/{operation_id}", headers=self.auth
            )
        ).json()
        self.assertEqual(value["status"], "cancelled")

    async def test_host_session_csrf_origin_and_reserved_slugs(self) -> None:
        rejected = await self.client.get(
            "/nitro/api/v1/info", headers={"Host": "evil.example"}
        )
        self.assertEqual(rejected.status, 400)
        page = await self.client.get("/nitro/", headers=self.host)
        html = await page.text()
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
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
            }
            for index in range(200)
        ]
        remote.extend(
            {
                "organizationSlug": "org",
                "competitionSlug": competition,
                "title": title,
            }
            for competition, title in (
                ("ready-error-unhealthy", "Ready error unhealthy"),
                ("ready-error-healthy", "Ready error healthy"),
                ("ready-missing", "Ready missing"),
                ("ready-running-unhealthy", "Ready running unhealthy"),
                ("ready-running-healthy", "Ready running healthy"),
                ("missing-error", "Missing error"),
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
                "org/ready-error-unhealthy",
                "org/ready-error-healthy",
                "org/ready-missing",
            ],
        )
        self.assertEqual(competitions[0]["image_state"], "ready")
        self.assertEqual(
            [item["reference"] for item in competitions[6:9]],
            ["org/missing-error", "org/filler-000", "org/filler-001"],
        )
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

    async def test_dashboard_paginates_and_searches_the_full_competition_list(self) -> None:
        response = await self.client.get("/nitro/assets/app.js", headers=self.host)
        script = await response.text()
        self.assertIn("const PAGE_SIZE = 25", script)
        self.assertIn("matches.slice(start, start + PAGE_SIZE)", script)
        self.assertIn("const match = !query ||", script)
        self.assertIn('refresh ? "?refresh=true" : ""', script)
        self.assertIn('addEventListener("click", () => load(true))', script)
        page = await self.client.get("/nitro/", headers=self.host)
        html = await page.text()
        self.assertIn('id="previous-page"', html)
        self.assertIn('id="next-page"', html)

    async def test_dashboard_ready_action_is_play_not_start(self) -> None:
        response = await self.client.get("/nitro/assets/app.js", headers=self.host)
        script = await response.text()
        self.assertIn('"_blank",\n          "noopener"', script)
        ready = script.split('workspace_state === "ready"', 1)[1].split("} else", 1)[0]
        self.assertIn('actionButton("Play", "play"', ready)
        self.assertNotIn('actionButton("Start", "start"', ready)

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
        lan = TestClient(
            TestServer(
                create_app(
                    backend=FakeBackend(),
                    store=lan_store,
                    api_token="api",
                    dashboard_token="dashboard-secret",
                    public_url="https://play.example:51123",
                    lan=True,
                )
            )
        )
        await lan.start_server()
        try:
            headers = {"Host": "play.example:51123"}
            page = await lan.get("/nitro/", headers=headers)
            self.assertIn("Protected network dashboard", await page.text())
            denied = await lan.post(
                "/nitro/login",
                headers=headers,
                data={"token": "wrong"},
                allow_redirects=False,
            )
            self.assertEqual(denied.status, 401)
            accepted = await lan.post(
                "/nitro/login",
                headers=headers,
                data={"token": "dashboard-secret"},
                allow_redirects=False,
            )
            self.assertEqual(accepted.status, 302)
            self.assertIn("Secure", accepted.headers.get("Set-Cookie", ""))
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
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    await socket.send_str(message.data)
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


if __name__ == "__main__":
    unittest.main()

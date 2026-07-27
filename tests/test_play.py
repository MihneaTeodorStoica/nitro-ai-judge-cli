from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from nitro_ai_judge_cli import cli, play, state
from nitro_ai_judge_cli.play_manager_client import ManagerClient
from nitro_ai_judge_cli.play_manager_lifecycle import (
    DockerEndpoint,
    generate_manager_compose,
    normalized_credentials,
    validate_manager_exposure,
)
from nitro_ai_judge_cli.play_protocol import WireError, validate_competition


class FakeClient:
    base_url = "http://localhost:51123"

    def __init__(self) -> None:
        self.actions: list[tuple[str, str, str, dict]] = []

    def info(self) -> dict:
        return {"identity": "naij-play-manager", "api_version": 1}

    def action(self, org: str, competition: str, action: str, **options: object) -> dict:
        self.actions.append((org, competition, action, dict(options)))
        return {"operation_id": "op-1"}

    def wait_operation(self, operation_id: str, **options: object) -> dict:
        return {
            "status": "complete",
            "result": {
                "reference": "org/contest",
                "workspace_state": "running",
                "service_health": "healthy",
                "jupyter_url": "/nitro/competitions/org/contest/jupyter/",
                "proxy_url": "/nitro/competitions/org/contest/proxy/",
            },
        }

    def competition(self, org: str, competition: str) -> dict:
        return self.wait_operation("x")["result"]

    def logs(self, org: str, competition: str, *, tail: int) -> dict:
        return {"logs": "safe logs", "tail": tail}

    def open_info(self, org: str, competition: str) -> dict:
        return {"jupyter_url": f"{self.base_url}/nitro/competitions/{org}/{competition}/jupyter/"}


class PlayProtocolTests(unittest.TestCase):
    def test_competition_slugs_are_canonical(self) -> None:
        self.assertEqual(validate_competition("org", "contest-1"), ("org", "contest-1"))
        self.assertEqual(
            validate_competition("nitro", "rise-2026-final"),
            ("nitro", "rise-2026-final"),
        )
        for value in ("Upper", "", "with space", "api"):
            with self.subTest(value=value), self.assertRaises(WireError):
                validate_competition("org", value)

    def test_competition_reference_forms(self) -> None:
        self.assertEqual(play.parse_competition_ref(["org/contest"]), ("org", "contest"))
        self.assertEqual(play.parse_competition_ref(["org", "contest"]), ("org", "contest"))
        with self.assertRaises(ValueError):
            play.parse_competition_ref(["contest"])

    def test_default_action_is_play_and_manager_is_preserved(self) -> None:
        self.assertEqual(play.normalize_play_argv([]), ["play"])
        self.assertEqual(play.normalize_play_argv(["org/contest"]), ["play", "org/contest"])
        self.assertEqual(play.normalize_play_argv(["manager", "status"]), ["manager", "status"])


class PlayCommandTests(unittest.TestCase):
    def test_yes_repair_preserves_saved_manager_configuration(self) -> None:
        config = {
            "bind": "0.0.0.0",
            "port": 54443,
            "image": "manager:saved",
            "tls_cert": "/saved/cert.pem",
            "tls_key": "/saved/key.pem",
            "public_url": "https://play.example:54443",
        }
        repaired = FakeClient()
        with (
            patch.object(play, "load_manager_config", return_value=config),
            patch.object(play, "install_manager", return_value={}) as install,
            patch.object(
                play.ManagerClient,
                "from_state",
                side_effect=[play.ManagerConnectionError("offline"), repaired],
            ),
        ):
            self.assertIs(play._client(yes=True), repaired)
        install.assert_called_once_with(
            bind="0.0.0.0",
            port=54443,
            image="manager:saved",
            tls_cert="/saved/cert.pem",
            tls_key="/saved/key.pem",
            public_url="https://play.example:54443",
            update=True,
        )

    def test_long_action_is_polled_and_returns_snapshot(self) -> None:
        client = FakeClient()
        result = play.perform_play_action(
            "org", "contest", "play", client=client, quiet=True, gpu=None, pull="missing"
        )
        self.assertEqual(result["workspace_state"], "running")
        self.assertEqual(client.actions[0][2], "play")
        self.assertEqual(client.actions[0][3]["pull"], "missing")

    def test_stop_and_container_delete_use_manager_actions(self) -> None:
        client = FakeClient()
        self.assertEqual(play.cmd_play_stop("org", "contest", client=client, quiet=True), 0)
        self.assertEqual(play.cmd_play_down("org", "contest", client=client, quiet=True), 0)
        self.assertEqual([item[2] for item in client.actions], ["stop", "delete-container"])

    def test_workspace_deletion_force_is_explicit(self) -> None:
        client = FakeClient()
        self.assertEqual(
            play.cmd_play_down(
                "org", "contest", client=client, quiet=True, volumes=True, force=True
            ),
            0,
        )
        self.assertEqual(client.actions[0][2], "delete-workspace")
        self.assertTrue(client.actions[0][3]["force"])

    def test_workspace_deletion_requires_force_without_tty(self) -> None:
        with patch.object(play.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "requires --force"):
                play.cmd_play_down("org", "contest", client=FakeClient(), volumes=True)

    def test_legacy_competition_ports_return_manager_guidance(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no longer published"):
            play.cmd_play_up(
                "org",
                "contest",
                gpu=None,
                port=8888,
                pull="missing",
                client=FakeClient(),
            )

    def test_status_maps_stable_urls(self) -> None:
        status = play.load_play_status("org", "contest", client=FakeClient(), logs=20)
        self.assertEqual(
            status["jupyter_url"],
            "http://localhost:51123/nitro/competitions/org/contest/jupyter/",
        )
        self.assertEqual(status["logs"], "safe logs")
        self.assertEqual(status["manager_url"], "http://localhost:51123/nitro/")

    def test_parser_exposes_manager_and_new_actions(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["play", "recreate", "org/contest", "--gpu", "--open"])
        self.assertEqual(args.play_action, "recreate")
        self.assertTrue(args.gpu)
        self.assertTrue(args.open)
        manager = parser.parse_args(["play", "manager", "install", "--yes"])
        self.assertEqual(manager.manager_action, "install")

    def test_cli_selected_context_is_reused(self) -> None:
        context = {"contest": {"organizationSlug": "org", "competitionSlug": "contest"}}
        with (
            patch.object(cli, "load_context", return_value=context),
            patch.object(cli, "cmd_play", return_value=0) as command,
        ):
            self.assertEqual(cli.main(["play", "stop"]), 0)
        self.assertEqual(command.call_args.args[0].competition, ["org/contest"])


class ManagerConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        state.configure_state_dir(self.tempdir.name)

    def tearDown(self) -> None:
        state.configure_state_dir(None)
        self.tempdir.cleanup()

    def test_compose_uses_secret_file_socket_network_and_labels(self) -> None:
        paths = state.ensure_state_dir().play_manager
        os.makedirs(paths)
        for name in ("cli-api-token", "dashboard-login-token"):
            with open(os.path.join(paths, name), "w", encoding="utf-8") as stream:
                stream.write("private")
        config = {
            "bind": "127.0.0.1",
            "port": 51123,
            "public_url": "http://localhost:51123",
            "image": "manager:test",
            "tls_cert": None,
            "tls_key": None,
            "dashboard_token": False,
        }
        compose = generate_manager_compose(
            config, DockerEndpoint("default", "unix:///run/docker.sock", "/run/docker.sock", "linux")
        )
        service = compose["services"]["manager"]
        self.assertIn("/run/docker.sock:/var/run/docker.sock", service["volumes"])
        self.assertEqual(service["secrets"], ["api-token"])
        self.assertEqual(compose["networks"]["nitro"]["name"], "naij-play")
        self.assertEqual(
            service["labels"]["org.nitro-ai.naij.play.owner"], "naij-play-manager"
        )
        self.assertNotIn("private", json.dumps(compose))

    def test_tls_healthcheck_uses_https_without_loopback_verification(self) -> None:
        paths = state.ensure_state_dir().play_manager
        os.makedirs(paths)
        config = {
            "bind": "127.0.0.1",
            "port": 51123,
            "public_url": "https://localhost:51123",
            "image": "manager:test",
            "tls_cert": "/cert.pem",
            "tls_key": "/key.pem",
            "dashboard_token": False,
        }
        compose = generate_manager_compose(
            config,
            DockerEndpoint(
                "default", "unix:///run/docker.sock", "/run/docker.sock", "linux"
            ),
        )
        script = compose["services"]["manager"]["healthcheck"]["test"][-1]
        self.assertIn("https://127.0.0.1:51123", script)
        self.assertIn("_create_unverified_context", script)

    def test_lan_requires_tls_and_https_public_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires"):
            validate_manager_exposure(
                "0.0.0.0", tls_cert=None, tls_key=None, public_url=None
            )
        validate_manager_exposure(
            "127.0.0.1", tls_cert=None, tls_key=None, public_url=None
        )

    def test_credentials_are_normalized_and_bounded(self) -> None:
        value = normalized_credentials(
            {"accessToken": "access", "refreshToken": "refresh", "username": "person", "cookies": ["ignored"]}
        )
        self.assertEqual(value["access_token"], "access")
        self.assertEqual(value["refresh_token"], "refresh")
        self.assertNotIn("cookies", value)


class ManagerClientTests(unittest.TestCase):
    def test_follow_logs_decodes_ndjson_to_plain_lines(self) -> None:
        class Response(list):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        response = Response([b'{"line":"first"}\n', b'{"line":"second"}\n'])
        with patch("urllib.request.urlopen", return_value=response):
            lines = list(ManagerClient("http://localhost", "token").follow_logs("org", "contest"))
        self.assertEqual(lines, ["first\n", "second\n"])

    def test_http_error_becomes_typed_wire_error(self) -> None:
        error = __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            "http://localhost", 409, "busy", {}, io.BytesIO(
                b'{"error":{"type":"competition_busy","message":"busy","stage":"applying","logs":[]}}'
            )
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(WireError) as caught:
                ManagerClient("http://localhost", "token").action(
                    "org", "contest", "play"
                )
        self.assertEqual(caught.exception.type, "competition_busy")
        self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nitro_ai_judge_cli import cli, play, play_manager_lifecycle, state, ui
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
        self.cancelled: list[str] = []
        self.latest_operation: dict | None = {
            "id": "op-1",
            "status": "running",
        }

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

    def competitions(self) -> list[dict]:
        return [
            {
                "reference": "org/contest",
                "operation": self.latest_operation,
            }
        ]

    def cancel(self, operation_id: str) -> dict:
        self.cancelled.append(operation_id)
        return {"id": operation_id, "status": "cancelled"}

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
    def test_bare_play_prints_ordered_first_run_checklist(self) -> None:
        output = io.StringIO()
        with (
            patch.object(cli, "load_context", return_value={}),
            patch.object(cli, "load_state", return_value=None),
            patch.object(cli, "cmd_play", side_effect=AssertionError("dispatched")),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.main(["play"]), 1)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "Complete Play setup:",
                "1. naij login",
                "2. naij use ORG/COMP",
                "3. naij play",
            ],
        )

    def test_bare_play_with_login_prints_final_two_steps(self) -> None:
        output = io.StringIO()
        with (
            patch.object(cli, "load_context", return_value={}),
            patch.object(cli, "load_state", return_value={"access_token": "token"}),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.main(["play"]), 1)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "Complete Play setup:",
                "1. naij use ORG/COMP",
                "2. naij play",
            ],
        )

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

    def test_client_forces_only_old_official_manager_images_forward(self) -> None:
        cases = (
            ("ghcr.io/mihneateodorstoica/naij-play-manager:3.0.3", True),
            (play.DEFAULT_MANAGER_IMAGE, False),
            ("ghcr.io/mihneateodorstoica/naij-play-manager:3.0.3-dev", False),
            ("registry.example/manager:3.0.3", False),
        )
        for configured, migrates in cases:
            with self.subTest(configured=configured):
                client = FakeClient()
                with (
                    patch.object(
                        play, "load_manager_config", return_value={"image": configured}
                    ),
                    patch.object(
                        play,
                        "_setup_manager",
                        return_value={"manager_version": "3.0.4"},
                    ) as setup,
                    patch.object(play.ManagerClient, "from_state", return_value=client),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertIs(play._client(interactive=False), client)
                if migrates:
                    self.assertEqual(
                        setup.call_args.kwargs["image"], play.DEFAULT_MANAGER_IMAGE
                    )
                    self.assertTrue(setup.call_args.kwargs["update"])
                else:
                    setup.assert_not_called()

    def test_manager_setup_spinner_stops_on_success_and_error(self) -> None:
        for outcome in ({"manager_version": "3.0.2"}, RuntimeError("failed")):
            with self.subTest(outcome=type(outcome).__name__):
                spinner = Mock()
                spinner.start.return_value = spinner
                with (
                    patch.object(play, "Spinner", return_value=spinner) as spinner_class,
                    patch.object(
                        play,
                        "install_manager",
                        side_effect=outcome if isinstance(outcome, Exception) else None,
                        return_value=outcome if isinstance(outcome, dict) else None,
                    ),
                ):
                    if isinstance(outcome, Exception):
                        with self.assertRaisesRegex(RuntimeError, "failed"):
                            play._setup_manager()
                    else:
                        self.assertEqual(play._setup_manager(), outcome)
                spinner_class.assert_called_once_with(
                    play.MANAGER_SETUP_LABEL, stream=play.sys.stdout
                )
                spinner.stop.assert_called_once_with()

    def test_manager_setup_ctrl_c_returns_130_after_spinner_cleanup(self) -> None:
        args = cli.build_parser().parse_args(
            ["play", "manager", "install", "--yes"]
        )
        spinner = Mock()
        spinner.start.return_value = spinner
        output = io.StringIO()
        with (
            patch.object(play, "Spinner", return_value=spinner),
            patch.object(play, "install_manager", side_effect=KeyboardInterrupt),
            redirect_stdout(output),
        ):
            self.assertEqual(play.cmd_play(args), 130)
        spinner.stop.assert_called_once_with()
        self.assertEqual(output.getvalue(), "Manager setup interrupted.\n")

    def test_spinner_frames_fit_narrow_terminal_and_cleanup(self) -> None:
        output = io.StringIO()
        output.isatty = lambda: True
        columns = 8
        with patch.object(
            ui.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((columns, 24)),
        ):
            for frame in ui.SPINNER_FRAMES:
                ui._draw_spinner(output, "Installing a manager with a long label", frame)
            ui.Spinner("unused", stream=output).stop()

        frames = output.getvalue().split(ui.CLEAR_ROW)[1:-1]
        self.assertEqual([value[-1] for value in frames], list(ui.SPINNER_FRAMES))
        self.assertTrue(all(len(value) < columns for value in frames))
        self.assertTrue(output.getvalue().endswith(ui.CLEAR_ROW))

    def test_manager_start_installs_when_missing_or_uninstalled(self) -> None:
        args = cli.build_parser().parse_args(["play", "manager", "start"])
        with (
            patch.object(play, "load_manager_config", return_value=None),
            patch.object(play, "_install_or_repair_manager") as repair,
            patch.object(play, "manager_container_exists") as container_exists,
            patch.object(play, "manager_compose_action") as compose_action,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(play.cmd_manager(args), 0)
        repair.assert_called_once_with()
        container_exists.assert_not_called()
        compose_action.assert_not_called()

        for exists in (False, True):
            with (
                self.subTest(container_exists=exists),
                patch.object(play, "load_manager_config", return_value={"port": 51123}),
                patch.object(
                    play_manager_lifecycle,
                    "run_process",
                    return_value=SimpleNamespace(stdout="container-id\n" if exists else ""),
                ) as process,
                patch.object(play, "_install_or_repair_manager") as repair,
                patch.object(play, "manager_compose_action") as compose_action,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(play.cmd_manager(args), 0)
            self.assertEqual(
                process.call_args.args[0][-4:], ["ps", "--all", "--quiet", "manager"]
            )
            if exists:
                compose_action.assert_called_once_with("start")
                repair.assert_not_called()
            else:
                repair.assert_called_once_with()
                compose_action.assert_not_called()

    def test_manager_lifecycle_forces_only_needed_migrations(self) -> None:
        for action, migrates in (
            ("start", True),
            ("restart", True),
            ("start", False),
            ("restart", False),
            ("stop", False),
        ):
            args = cli.build_parser().parse_args(["play", "manager", action])
            with (
                self.subTest(action=action, migrates=migrates),
                patch.object(
                    play, "_migrate_manager_if_needed", return_value=migrates
                ) as migrate,
                patch.object(play, "load_manager_config", return_value={"image": "saved"}),
                patch.object(play, "manager_container_exists", return_value=True),
                patch.object(play, "manager_compose_action") as compose_action,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(play.cmd_manager(args), 0)
            if action == "stop":
                migrate.assert_not_called()
            else:
                migrate.assert_called_once_with()
            if migrates:
                compose_action.assert_not_called()
            else:
                compose_action.assert_called_once_with(action)

    def test_long_action_is_polled_and_returns_snapshot(self) -> None:
        client = FakeClient()
        result = play.perform_play_action(
            "org", "contest", "play", client=client, quiet=True, gpu=None, pull="missing"
        )
        self.assertEqual(result["workspace_state"], "running")
        self.assertEqual(client.actions[0][2], "play")
        self.assertEqual(client.actions[0][3]["pull"], "missing")

    def test_interactive_progress_updates_one_spinner(self) -> None:
        class ProgressClient(FakeClient):
            def wait_operation(self, operation_id: str, **options: object) -> dict:
                options["progress"](
                    {"message": "Pulling contest image 1/2 (3s elapsed)"}
                )
                return super().wait_operation(operation_id, **options)

        output = io.StringIO()
        output.isatty = lambda: True
        with (
            patch.object(play.sys, "stdout", output),
            patch.object(play, "Spinner") as spinner_class,
        ):
            spinner = spinner_class.return_value
            spinner.start.return_value = spinner
            play.perform_play_action("org", "contest", "play", client=ProgressClient())

        spinner.update.assert_called_once_with(
            "Pulling contest image 1/2 (3s elapsed)"
        )
        spinner.stop.assert_called_once_with()

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

        delete_image = parser.parse_args(
            ["play", "delete-image", "org/contest", "--yes"]
        )
        client = FakeClient()
        with patch.object(play, "_client", return_value=client):
            self.assertEqual(play.cmd_play(delete_image), 0)
        self.assertEqual(client.actions[0][2], "delete-image")

        cancel = parser.parse_args(["play", "cancel", "org/contest"])
        self.assertEqual(cancel.play_action, "cancel")
        help_output = io.StringIO()
        with redirect_stdout(help_output), self.assertRaises(SystemExit) as shown:
            parser.parse_args(["play", "--help"])
        self.assertEqual(shown.exception.code, 0)
        self.assertIn("cancel", help_output.getvalue())

    def test_cancel_defaults_to_selected_contest(self) -> None:
        context = {
            "contest": {
                "organizationSlug": "org",
                "competitionSlug": "contest",
            }
        }
        client = FakeClient()
        with (
            patch.object(cli, "load_context", return_value=context),
            patch.object(play, "_client", return_value=client),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.main(["play", "cancel"]), 0)
        self.assertEqual(client.cancelled, ["op-1"])

    def test_cancel_reports_inactive_and_completed_race(self) -> None:
        client = FakeClient()
        client.latest_operation = {"id": "op-1", "status": "complete"}
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                play.cmd_play_cancel("org", "contest", client=client), 1
            )
        self.assertEqual(client.cancelled, [])
        self.assertIn("No active operation", output.getvalue())

        client.latest_operation = {"id": "op-2", "status": "running"}
        client.cancel = Mock(return_value={"id": "op-2", "status": "complete"})
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                play.cmd_play_cancel("org", "contest", client=client), 0
            )
        self.assertIn("already completed", output.getvalue())

    def test_ctrl_c_while_waiting_prints_non_cancelling_guidance(self) -> None:
        client = FakeClient()
        client.wait_operation = Mock(side_effect=KeyboardInterrupt)
        args = cli.build_parser().parse_args(
            ["play", "play", "org/contest"]
        )
        output = io.StringIO()
        with patch.object(play, "_client", return_value=client), redirect_stdout(output):
            self.assertEqual(play.cmd_play(args), 130)
        self.assertEqual(
            output.getvalue().splitlines()[-2:],
            [
                "Status: naij play status org/contest",
                "Cancel: naij play cancel org/contest",
            ],
        )
        self.assertEqual(client.cancelled, [])

    def test_ctrl_c_outside_wait_has_no_cancellation_guidance(self) -> None:
        args = cli.build_parser().parse_args(
            ["play", "delete-image", "org/contest"]
        )
        output = io.StringIO()
        with (
            patch.object(play.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", side_effect=KeyboardInterrupt),
            redirect_stdout(output),
        ):
            self.assertEqual(play.cmd_play(args), 130)
        self.assertEqual(output.getvalue(), "Interrupted.\n")

    def test_image_deletion_requires_yes_without_tty(self) -> None:
        args = cli.build_parser().parse_args(
            ["play", "delete-image", "org/contest"]
        )
        with patch.object(play.sys.stdin, "isatty", return_value=False):
            self.assertEqual(play.cmd_play(args), 1)

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

    def test_update_restores_config_when_compose_write_fails(self) -> None:
        root = state.ensure_state_dir().play_manager
        os.makedirs(root)
        paths = play_manager_lifecycle.manager_paths()
        old_config = {
            "schema": 1,
            "image": "manager:old",
            "bind": "127.0.0.1",
            "port": 51123,
            "public_url": "http://localhost:51123",
            "tls_cert": None,
            "tls_key": None,
            "dashboard_token": False,
            "docker_context": "default",
            "docker_host": "unix:///run/docker.sock",
        }
        old_compose = b'{"services":{"manager":{"image":"manager:old"}}}\n'
        state.atomic_write(
            paths["config"],
            (json.dumps(old_config, indent=2, sort_keys=True) + "\n").encode(),
        )
        state.atomic_write(paths["compose"], old_compose)
        real_atomic_write = state.atomic_write
        writes = 0

        def fail_second_write(path: str, data: bytes, **options: object) -> None:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("disk full")
            real_atomic_write(path, data, **options)

        endpoint = DockerEndpoint(
            "default", "unix:///run/docker.sock", "/run/docker.sock", "linux"
        )
        process = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(play_manager_lifecycle, "resolve_docker_endpoint", return_value=endpoint),
            patch.object(play_manager_lifecycle, "_port_in_use", return_value=False),
            patch.object(play_manager_lifecycle, "_write_secret"),
            patch.object(play_manager_lifecycle, "run_process", return_value=process) as run,
            patch.object(
                play_manager_lifecycle, "atomic_write", side_effect=fail_second_write
            ),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            play_manager_lifecycle.install_manager(image="manager:new", update=True)

        with open(paths["config"], encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), old_config)
        with open(paths["compose"], "rb") as stream:
            self.assertEqual(stream.read(), old_compose)
        self.assertEqual(run.call_args.args[0][-3:], ["up", "-d", "--remove-orphans"])

    def test_credentials_are_normalized_and_bounded(self) -> None:
        value = normalized_credentials(
            {"accessToken": "access", "refreshToken": "refresh", "username": "person", "cookies": ["ignored"]}
        )
        self.assertEqual(value["access_token"], "access")
        self.assertEqual(value["refresh_token"], "refresh")
        self.assertNotIn("cookies", value)


class ManagerClientTests(unittest.TestCase):
    def test_wait_operation_honors_local_stop_without_remote_cancel(self) -> None:
        client = ManagerClient("http://localhost", "token")
        stop_event = threading.Event()
        stop_event.set()
        with (
            patch.object(client, "operation") as operation,
            self.assertRaises(InterruptedError),
        ):
            client.wait_operation("operation", stop_event=stop_event)
        operation.assert_not_called()

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

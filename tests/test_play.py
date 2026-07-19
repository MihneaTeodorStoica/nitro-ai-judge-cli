import os
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from nitro_ai_judge_cli import cli
from nitro_ai_judge_cli import play as nitro_cli


class PlayCommandTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_play_state_dir = nitro_cli.PLAY_STATE_DIR

    def tearDown(self):
        nitro_cli.PLAY_STATE_DIR = self.original_play_state_dir
        self.tempdir.cleanup()

    def make_environment(self):
        nitro_cli.PLAY_STATE_DIR = self.tempdir.name
        workdir = nitro_cli.play_workdir("algolymp", "contest")
        os.makedirs(workdir, exist_ok=True)
        with open(
            os.path.join(workdir, "docker-compose.yml"), "w", encoding="utf-8"
        ) as f:
            f.write("services: {}\n")
        return workdir

    def test_play_down_preserves_workspace_by_default(self):
        workdir = self.make_environment()
        calls = []

        def fake_run_process(cmd, *, cwd=None, check=True):
            calls.append((cmd, cwd, check))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch.object(nitro_cli, "run_process", side_effect=fake_run_process),
            patch.object(nitro_cli, "ensure_docker_ready"),
        ):
            result = nitro_cli.cmd_play_down("algolymp", "contest")
        self.assertEqual(result, 0)
        self.assertTrue(os.path.exists(workdir))
        self.assertEqual(calls[-1][0][-2:], ["down", "--remove-orphans"])

    def test_play_down_volumes_force_adds_compose_flag(self):
        self.make_environment()
        with (
            patch.object(
                nitro_cli,
                "run_process",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ) as run,
            patch.object(nitro_cli, "ensure_docker_ready"),
        ):
            self.assertEqual(
                nitro_cli.cmd_play_down(
                    "algolymp", "contest", volumes=True, force=True
                ),
                0,
            )
        self.assertEqual(
            run.call_args_list[-1].args[0][-3:],
            ["down", "--remove-orphans", "--volumes"],
        )

    def test_play_down_volumes_requires_force_without_tty(self):
        self.make_environment()
        with (
            patch.object(nitro_cli, "ensure_docker_ready"),
            patch.object(nitro_cli.sys.stdin, "isatty", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "requires --force"):
                nitro_cli.cmd_play_down("algolymp", "contest", volumes=True)

    def test_play_down_reports_missing_environment(self):
        nitro_cli.PLAY_STATE_DIR = self.tempdir.name
        with patch.object(nitro_cli, "ensure_docker_ready"):
            result = nitro_cli.cmd_play_down("algolymp", "contest")
        self.assertEqual(result, 0)

    def test_parser_actions_and_gpu_conflict(self):
        parser = cli.build_parser()
        args = parser.parse_args(["play", "logs", "algolymp/contest", "--follow"])
        self.assertEqual(
            (args.play_action, args.competition, args.follow),
            ("logs", ["algolymp/contest"], True),
        )
        with self.assertRaises(SystemExit):
            parser.parse_args(["play", "up", "algolymp/contest", "--gpu", "--no-gpu"])

    def test_play_cli_delegates_stop_and_down(self):
        self.make_environment()
        args = SimpleNamespace(play_action="stop", competition=["algolymp/contest"])
        with patch.object(nitro_cli, "cmd_play_stop", return_value=0) as stop:
            self.assertEqual(nitro_cli.cmd_play(args), 0)
        stop.assert_called_once_with("algolymp", "contest")

        args = SimpleNamespace(
            play_action="down",
            competition=["algolymp/contest"],
            volumes=True,
            force=True,
        )
        with patch.object(nitro_cli, "cmd_play_down", return_value=0) as down:
            self.assertEqual(nitro_cli.cmd_play(args), 0)
        down.assert_called_once_with("algolymp", "contest", volumes=True, force=True)

    def test_pull_policies(self):
        def result(code=0):
            return SimpleNamespace(returncode=code, stdout="", stderr="")

        with patch.object(
            nitro_cli,
            "run_process",
            side_effect=[result(1), result(1), result(), result()],
        ) as run:
            nitro_cli.ensure_play_images(("notebook", "proxy"), "missing")
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["docker", "image", "inspect", "notebook"],
                ["docker", "image", "inspect", "proxy"],
                ["docker", "pull", "notebook"],
                ["docker", "pull", "proxy"],
            ],
        )
        with patch.object(nitro_cli, "run_process", return_value=result(1)):
            with self.assertRaisesRegex(RuntimeError, "--pull=never"):
                nitro_cli.ensure_play_images(("notebook", "proxy"), "never")

    def test_pull_progress_is_plain_when_redirected_and_skips_present_images(self):
        def result(code=0):
            return SimpleNamespace(returncode=code, stdout="", stderr="")

        output = io.StringIO()
        with (
            patch.object(
                nitro_cli,
                "run_process",
                side_effect=[result(), result(1), result()],
            ),
            redirect_stdout(output),
        ):
            nitro_cli.ensure_play_images(("present", "missing"), "missing")
        self.assertEqual(
            output.getvalue(),
            "Pulling image 1/1: missing\nPulled image: missing\n",
        )
        self.assertNotIn("\r", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())

        output = io.StringIO()
        with (
            patch.object(nitro_cli, "run_process", return_value=result()),
            redirect_stdout(output),
        ):
            nitro_cli.ensure_play_images(("present",), "missing")
        self.assertEqual(output.getvalue(), "")

    def test_pull_failure_clears_tty_spinner_before_raising(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True

        def result(code=0):
            return SimpleNamespace(returncode=code, stdout="", stderr="")

        output = TTY()
        with (
            patch.object(
                nitro_cli,
                "run_process",
                side_effect=[result(1), RuntimeError("pull failed")],
            ),
            patch.object(nitro_cli.sys, "stdout", output),
        ):
            with self.assertRaisesRegex(RuntimeError, "pull failed"):
                nitro_cli.ensure_play_images(("missing",), "missing")
        self.assertIn("Pulling image 1/1: missing", output.getvalue())
        self.assertTrue(output.getvalue().endswith("\r\x1b[K"))

    def test_port_allocation_scans_and_explicit_fails(self):
        with patch.object(nitro_cli, "port_is_free", side_effect=[False, True]):
            self.assertEqual(nitro_cli.allocate_port("127.0.0.1", 8888, None), 8889)
        with patch.object(nitro_cli, "port_is_free", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                nitro_cli.allocate_port("127.0.0.1", 7777, 7777)

    def test_gpu_modes(self):
        with patch.object(nitro_cli, "run_process") as run:
            self.assertEqual(
                nitro_cli.resolve_play_gpu("image", False), (False, "disabled")
            )
            run.assert_not_called()
        failure = SimpleNamespace(returncode=1, stdout="", stderr="no runtime")
        with (
            patch.object(nitro_cli, "run_process", return_value=failure),
            patch("builtins.print"),
        ):
            self.assertEqual(
                nitro_cli.resolve_play_gpu("image", None), (False, "no runtime")
            )
            with self.assertRaisesRegex(RuntimeError, "GPU requested"):
                nitro_cli.resolve_play_gpu("image", True)

    def test_generated_compose_is_project_scoped_and_persistent(self):
        nitro_cli.PLAY_STATE_DIR = self.tempdir.name
        workdir = nitro_cli.write_play_files(
            "org",
            "contest",
            8888,
            9000,
            "127.0.0.1",
            "cpu",
            False,
            ("note:image", "proxy:image"),
        )
        with open(os.path.join(workdir, "docker-compose.yml"), encoding="utf-8") as f:
            compose = f.read()
        self.assertIn("workspace:/home/jovyan", compose)
        self.assertIn("volumes:\n  workspace:", compose)
        self.assertNotIn("external: true", compose)
        self.assertNotIn("deploy:", compose)
        self.assertEqual(os.stat(os.path.join(workdir, ".env")).st_mode & 0o777, 0o600)
        self.assertEqual(
            os.stat(
                os.path.join(workdir, "secrets", "session_whitelist_bypass_key")
            ).st_mode
            & 0o777,
            0o600,
        )
        self.assertEqual(
            nitro_cli.read_play_env("org", "contest")["JUPYTER_PORT"], "8888"
        )

        gpu_workdir = nitro_cli.write_play_files(
            "org",
            "gpu-contest",
            8889,
            9001,
            "127.0.0.1",
            "gpu",
            True,
            ("note:image", "proxy:image"),
        )
        with open(
            os.path.join(gpu_workdir, "docker-compose.yml"), encoding="utf-8"
        ) as f:
            gpu_compose = f.read()
        self.assertIn("driver: nvidia", gpu_compose)
        self.assertIn("capabilities: [gpu]", gpu_compose)

    def test_legacy_workspace_migration_and_rollback(self):
        self.make_environment()
        inspect = '[{"State":{"Running":true},"Mounts":[]}]'
        responses = [
            SimpleNamespace(returncode=0, stdout="legacy-id\n", stderr=""),
            SimpleNamespace(returncode=0, stdout=inspect, stderr=""),
        ]
        with patch.object(
            nitro_cli,
            "run_process",
            side_effect=[
                *responses,
                *[SimpleNamespace(returncode=0, stdout="", stderr="")] * 6,
            ],
        ) as run:
            nitro_cli.migrate_legacy_workspace("algolymp", "contest", "notebook")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            [
                "docker",
                "cp",
                "--archive",
                "legacy-id:/home/jovyan/.",
                "nitro-algolymp-contest-workspace-migration:/home/jovyan",
            ],
            commands,
        )
        self.assertEqual(
            commands[-1], ["docker", "rm", "nitro-algolymp-contest-workspace-migration"]
        )

        failure = RuntimeError("copy failed")
        side_effect = [
            *responses,
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            failure,
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
        with patch.object(nitro_cli, "run_process", side_effect=side_effect) as run:
            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                nitro_cli.migrate_legacy_workspace("algolymp", "contest", "notebook")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            ["docker", "volume", "rm", "nitro-algolymp-contest_workspace"], commands
        )
        self.assertEqual(commands[-1], ["docker", "start", "legacy-id"])

    def test_logs_follow_is_optional(self):
        self.make_environment()
        completed = SimpleNamespace(returncode=0)
        with (
            patch.object(nitro_cli, "ensure_docker_ready"),
            patch.object(nitro_cli.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(nitro_cli.cmd_play_logs("algolymp", "contest"), 0)
            self.assertNotIn("-f", run.call_args.args[0])
            self.assertEqual(
                nitro_cli.cmd_play_logs("algolymp", "contest", follow=True), 0
            )
            self.assertEqual(run.call_args.args[0][-1], "-f")

    def test_status_reports_saved_configuration(self):
        nitro_cli.PLAY_STATE_DIR = self.tempdir.name
        nitro_cli.write_play_files(
            "org",
            "contest",
            8888,
            9000,
            "127.0.0.1",
            "auto",
            False,
            ("note:image", "proxy:image"),
        )
        args = SimpleNamespace(play_action="status", competition=["org/contest"])
        state = SimpleNamespace(returncode=0, stdout='[{"State":"running"}]', stderr="")
        with (
            patch.object(nitro_cli, "run_process", return_value=state),
            patch("builtins.print") as output,
        ):
            self.assertEqual(nitro_cli.cmd_play(args), 0)
        text = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("State: running", text)
        self.assertIn("GPU: auto (effective cpu)", text)
        self.assertIn("nitro-org-contest_workspace", text)

    def test_shell_play_uses_selected_contest(self):
        context = {
            "contest": {
                "organizationSlug": "algolymp",
                "competitionSlug": "contest",
            }
        }
        with (
            patch.object(cli, "load_context", return_value=context),
            patch.object(cli, "cmd_play", return_value=0) as play,
        ):
            self.assertEqual(cli.main(["play", "stop"]), 0)
        args = play.call_args.args[0]
        self.assertEqual(args.competition, ["algolymp/contest"])
        self.assertEqual(args.play_action, "stop")


if __name__ == "__main__":
    unittest.main()

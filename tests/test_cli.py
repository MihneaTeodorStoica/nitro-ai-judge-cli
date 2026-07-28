from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nitro_ai_judge_cli import __version__, cli, state  # noqa: E402


def invoke(function, argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            result = function(argv)
        except SystemExit as exc:
            result = int(exc.code or 0)
    return int(result), stdout.getvalue(), stderr.getvalue()


class EntrypointTests(unittest.TestCase):
    def test_version_entrypoints_report_installed_version_without_runtime_setup(self) -> None:
        with patch.object(cli, "configure_runtime", side_effect=AssertionError("runtime")):
            canonical = invoke(cli.main, ["--version"])
            short = invoke(cli.main, ["-V"])
            legacy = invoke(cli.legacy_main, ["--version"])

        self.assertEqual(canonical, (0, f"naij {__version__}\n", ""))
        self.assertEqual(short, canonical)
        self.assertEqual(legacy[:2], canonical[:2])
        self.assertEqual(legacy[2], cli.LEGACY_WARNING + "\n")

    def test_legacy_help_has_canonical_output_and_exact_warning(self) -> None:
        canonical = invoke(cli.main, ["--help"])
        legacy = invoke(cli.legacy_main, ["--help"])

        self.assertEqual(canonical[0], 0)
        self.assertEqual(legacy[0], canonical[0])
        self.assertEqual(legacy[1], canonical[1])
        self.assertEqual(canonical[2], "")
        self.assertEqual(legacy[2], cli.LEGACY_WARNING + "\n")
        self.assertIn("usage: naij", canonical[1])

    def test_legacy_delegates_unchanged_arguments_and_exit_status(self) -> None:
        stderr = io.StringIO()
        with patch.object(cli, "main", return_value=17) as main:
            with contextlib.redirect_stderr(stderr):
                result = cli.legacy_main(["completion", "bash"])

        self.assertEqual(result, 17)
        main.assert_called_once_with(["completion", "bash"])
        self.assertEqual(stderr.getvalue(), cli.LEGACY_WARNING + "\n")

    def test_completion_command_has_entrypoint_parity(self) -> None:
        canonical = invoke(cli.main, ["completion", "fish"])
        legacy = invoke(cli.legacy_main, ["completion", "fish"])

        self.assertEqual(canonical[:2], legacy[:2])
        self.assertEqual(legacy[2], cli.LEGACY_WARNING + "\n")

    def test_internal_play_completion_keeps_the_current_slot_unchanged(self) -> None:
        with patch.object(cli, "completion_candidates", return_value=["up"]) as candidates:
            result = invoke(cli.main, ["__complete", "--", "play", ""])

        self.assertEqual(result, (0, "up\n", ""))
        candidates.assert_called_once_with(["play", ""])

    def test_python_module_runs_canonical_help(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-m", "nitro_ai_judge_cli", "--help"],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: naij", result.stdout)
        self.assertNotIn("deprecated", result.stderr)

    def test_python_module_reports_canonical_version(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-m", "nitro_ai_judge_cli", "--version"],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"naij {__version__}\n")


class CredentialCommandTests(unittest.TestCase):
    def tearDown(self) -> None:
        state.configure_state_dir(None)

    def test_password_stdin_reads_one_line(self) -> None:
        with (
            patch.object(cli.sys, "stdin", io.StringIO("secret phrase\nignored\n")),
            patch.object(cli, "cmd_login", return_value=1) as login,
        ):
            result = invoke(cli.main, ["login", "--username", "alice", "--password-stdin"])

        self.assertEqual(result, (1, "", ""))
        login.assert_called_once_with("alice", "secret phrase")

    def test_password_stdin_rejects_empty_and_interactive_input(self) -> None:
        with patch.object(cli.sys, "stdin", io.StringIO("secret\n")), patch.object(
            cli, "cmd_login"
        ) as login:
            missing_username = invoke(cli.main, ["login", "--password-stdin"])
        self.assertEqual(missing_username[0], 2)
        self.assertIn("--username is required", missing_username[2])
        login.assert_not_called()

        with patch.object(cli.sys, "stdin", io.StringIO("")), patch.object(
            cli, "cmd_login"
        ) as login:
            empty = invoke(
                cli.main, ["login", "--username", "alice", "--password-stdin"]
            )
        self.assertEqual(empty[0], 2)
        self.assertIn("empty password", empty[2])
        login.assert_not_called()

        terminal = io.StringIO("secret\n")
        terminal.isatty = lambda: True  # type: ignore[method-assign]
        with patch.object(cli.sys, "stdin", terminal), patch.object(
            cli, "cmd_login"
        ) as login:
            interactive = invoke(
                cli.main, ["login", "--username", "alice", "--password-stdin"]
            )
        self.assertEqual(interactive[0], 2)
        self.assertIn("requires piped input", interactive[2])
        login.assert_not_called()

    def test_logout_removes_only_custom_root_credentials_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state.configure_state_dir(str(root))
            state.save_state({"access_token": "secret"})
            state.save_context({"contest": {"org": "o", "comp": "c"}})
            (root / "history").write_text("kept", encoding="utf-8")
            (root / "play-manager").mkdir()
            (root / "play-manager" / "manager.json").write_text("{}", encoding="utf-8")

            first = invoke(cli.main, ["--state-dir", str(root), "logout"])
            second = invoke(cli.main, ["--state-dir", str(root), "logout"])

            self.assertEqual(first[0], 0)
            self.assertIn("Logged out", first[1])
            self.assertIn("Disconnect Nitro", first[1])
            self.assertEqual(second[0], 0)
            self.assertIn("Already logged out", second[1])
            self.assertFalse((root / "state.json").exists())
            self.assertTrue((root / "context.json").exists())
            self.assertEqual((root / "history").read_text(encoding="utf-8"), "kept")
            self.assertTrue((root / "play-manager" / "manager.json").exists())

    def test_logout_reports_credential_removal_failure(self) -> None:
        with patch.object(
            cli,
            "clear_credentials",
            side_effect=state.CredentialsError("permission denied"),
        ):
            result = invoke(cli.main, ["logout"])

        self.assertEqual(result[0], 1)
        self.assertIn("permission denied", result[2])


class ParserTests(unittest.TestCase):
    def test_submit_short_flags_and_explicit_target_forms(self) -> None:
        parser = cli.build_parser()
        slash = parser.parse_args(
            [
                "submit",
                "org/contest",
                "task",
                "-o",
                "answer.csv",
                "-s",
                "source.py",
                "-n",
                "note",
                "-w",
            ]
        )
        split = parser.parse_args(["submit", "org", "contest", "task", "-o", "answer.csv"])
        contextual = parser.parse_args(["submit", "task", "-o", "answer.csv"])

        self.assertEqual(slash.targets, ["org/contest", "task"])
        self.assertEqual(split.targets, ["org", "contest", "task"])
        self.assertEqual(contextual.targets, ["task"])
        self.assertEqual(slash.output, "answer.csv")
        self.assertEqual(slash.source, "source.py")
        self.assertEqual(slash.note, "note")
        self.assertTrue(slash.wait)
        self.assertEqual(slash.wait_timeout, 180)

        existing = parser.parse_args(
            ["submission", "submission-id", "--wait", "--wait-timeout", "45"]
        )
        self.assertTrue(existing.wait)
        self.assertEqual(existing.wait_timeout, 45)

    def test_download_and_submission_list_short_flags(self) -> None:
        parser = cli.build_parser()
        download = parser.parse_args(
            [
                "download-data",
                "org/contest",
                "task",
                "-c",
                "statement",
                "-d",
                "data",
                "-o",
                "statement.pdf",
                "-f",
            ]
        )
        submissions = parser.parse_args(
            [
                "submissions",
                "org/contest",
                "task",
                "-a",
                "user",
                "-p",
                "3",
                "-n",
                "50",
                "-m",
                "complete",
            ]
        )

        self.assertEqual(download.category, ["statement"])
        self.assertEqual(download.out_dir, "data")
        self.assertEqual(download.output, "statement.pdf")
        self.assertTrue(download.force)
        self.assertEqual(submissions.author, "user")
        self.assertEqual(submissions.page, 3)
        self.assertEqual(submissions.page_size, 50)
        self.assertEqual(submissions.mode, "complete")

    def test_global_state_directory_flag_is_parsed(self) -> None:
        args = cli.build_parser().parse_args(["--state-dir", "/tmp/naij", "use"])
        self.assertEqual(args.state_dir, "/tmp/naij")

    def test_task_target_resolution_prefers_explicit_values_without_mutation(self) -> None:
        context = {
            "contest": {"organizationSlug": "stored", "competitionSlug": "contest"},
            "task": {"id": "stored-task"},
        }

        self.assertEqual(
            cli._resolve_task_target([], context),
            ("stored", "contest", "stored-task", True),
        )
        self.assertEqual(
            cli._resolve_task_target(["explicit-task"], context),
            ("stored", "contest", "explicit-task", False),
        )
        self.assertEqual(
            cli._resolve_task_target(["other/contest", "task"], context),
            ("other", "contest", "task", False),
        )
        self.assertEqual(
            cli._resolve_task_target(["other", "contest", "task"], context),
            ("other", "contest", "task", False),
        )
        self.assertEqual(
            cli._resolve_task_target(["stored/contest"], context),
            ("stored", "contest", "stored-task", True),
        )
        with self.assertRaisesRegex(ValueError, "task is missing"):
            cli._resolve_task_target(["other/contest"], context)
        self.assertEqual(context["task"], {"id": "stored-task"})

    def test_context_only_task_requires_a_selected_contest(self) -> None:
        with self.assertRaisesRegex(ValueError, "contest is missing"):
            cli._resolve_task_target(["task"], {})
        with self.assertRaisesRegex(ValueError, "no task selected"):
            cli._resolve_task_target([], {})


class ContextDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {"HOME": str(self.root), "NAIJ_STATE_DIR": str(self.root / "state")},
            clear=True,
        )
        self.environment.start()
        state.reset_state_paths()
        state._warned.clear()

    def tearDown(self) -> None:
        state.reset_state_paths()
        state._warned.clear()
        self.environment.stop()
        self.temporary.cleanup()

    def test_use_restores_changes_and_invalidates_dependent_context(self) -> None:
        state.set_contest({"organizationSlug": "old", "competitionSlug": "contest"})
        state.set_task({"id": "old-task"})
        state.set_submission("old-submission")
        tasks = [{"id": "new-task", "title": "New Task"}, {"id": "2", "title": "Two"}]

        with patch.object(cli, "require_auth", return_value=({}, ("", ""), "token")):
            with patch.object(cli, "load_tasks", return_value=tasks):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = cli.main(["use", "new/contest", "NEW TASK"])

        context = state.load_context()
        self.assertEqual(result, 0)
        self.assertEqual(state.selected_contest(context), ("new", "contest"))
        self.assertEqual(state.selected_task(context), "new-task")
        self.assertIsNone(state.selected_submission(context))
        self.assertEqual(
            state.cached_items("tasks", "new/contest"),
            tasks,
        )

    def test_use_task_only_keeps_selected_contest(self) -> None:
        state.set_contest({"organizationSlug": "org", "competitionSlug": "contest"})
        tasks = [{"id": "3", "title": "First"}, {"id": "4", "title": "Second"}]
        stdout = io.StringIO()

        with patch.object(cli, "require_auth", return_value=({}, ("", ""), "token")):
            with patch.object(cli, "load_tasks", return_value=tasks):
                with contextlib.redirect_stdout(stdout):
                    result = cli.main(["use", "2"])

        context = state.load_context()
        self.assertEqual(result, 0)
        self.assertEqual(state.selected_contest(context), ("org", "contest"))
        self.assertEqual(state.selected_task(context), "4")
        self.assertIn("Task: 2 (Second)", stdout.getvalue())

    def test_explicit_task_number_resolves_to_backend_id(self) -> None:
        tasks = [{"id": "3"}, {"id": "4"}, {"id": "6"}]
        auth = ({}, ("", ""), "token")
        with patch.object(cli, "require_auth", return_value=auth), patch.object(
            cli, "load_tasks", return_value=tasks
        ), patch.object(cli, "cmd_task", return_value=0) as command:
            self.assertEqual(
                cli.main(["task", "ceoai/ceoai-2026-day-1", "3"]), 0
            )

        command.assert_called_once_with(
            ("", ""), "token", "ceoai", "ceoai-2026-day-1", "6", display_id="3"
        )

    def test_failed_use_does_not_change_existing_selection(self) -> None:
        state.set_contest({"organizationSlug": "old", "competitionSlug": "contest"})
        state.set_task({"id": "old-task"})
        before = state.load_context()

        with patch.object(cli, "require_auth", return_value=({}, ("", ""), "token")):
            with patch.object(cli, "load_tasks", return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = cli.main(["use", "new/contest", "missing"])

        self.assertEqual(result, 1)
        self.assertEqual(state.load_context(), before)

    def test_use_without_arguments_shows_context_without_authentication(self) -> None:
        state.set_contest({"organizationSlug": "org", "competitionSlug": "contest"})
        state.set_task({"id": "1"})
        stdout = io.StringIO()

        with patch.object(cli, "require_auth", side_effect=AssertionError("auth")):
            with contextlib.redirect_stdout(stdout):
                result = cli.main(["use"])

        self.assertEqual(result, 0)
        self.assertIn("Contest: org/contest", stdout.getvalue())
        self.assertIn("Task: 1", stdout.getvalue())

    def test_ls_dispatches_by_context_depth(self) -> None:
        auth = ({"username": "user"}, ("", ""), "token")
        contexts = [
            ({}, "cmd_contests"),
            ({"contest": {"org": "o", "comp": "c"}}, "cmd_tasks"),
            (
                {"contest": {"org": "o", "comp": "c"}, "task": {"id": "1"}},
                "cmd_submissions",
            ),
        ]
        for context, expected in contexts:
            with self.subTest(expected=expected):
                with patch.object(cli, "require_auth", return_value=auth):
                    with patch.object(cli, "load_context", return_value=context):
                        with patch.object(cli, expected, return_value=23) as command:
                            result = cli.main(["ls"])
                self.assertEqual(result, 23)
                command.assert_called_once()

    def test_submission_wait_uses_selected_id_and_timeout(self) -> None:
        auth = ({}, ("", ""), "token")
        context = {
            "contest": {"org": "o", "comp": "c"},
            "task": {"id": "1"},
            "submission": {"id": "selected-id"},
        }
        with patch.object(cli, "require_auth", return_value=auth), patch.object(
            cli, "load_context", return_value=context
        ), patch.object(cli, "cmd_submission", return_value=0) as command:
            result = cli.main(["submission", "--wait", "--wait-timeout", "45"])

        self.assertEqual(result, 0)
        command.assert_called_once_with(
            ("", ""),
            "token",
            "selected-id",
            org="o",
            comp="c",
            task_id="1",
            wait=True,
            wait_timeout=45,
        )

    def test_show_dispatches_submission_task_and_contest_levels(self) -> None:
        auth = ({}, ("", ""), "token")
        submission_context = {
            "contest": {"org": "o", "comp": "c"},
            "task": {"id": "1"},
            "submission": {"id": "submission-id"},
        }
        with patch.object(cli, "require_auth", return_value=auth), patch.object(
            cli, "load_context", return_value=submission_context
        ), patch.object(cli, "cmd_submission", return_value=7) as command:
            self.assertEqual(cli.main(["show"]), 7)
        command.assert_called_once()

        task_context = {
            "contest": {"org": "o", "comp": "c"},
            "task": {"id": "1"},
        }
        with patch.object(cli, "require_auth", return_value=auth), patch.object(
            cli, "load_context", return_value=task_context
        ), patch.object(cli, "cmd_task", return_value=8) as command:
            self.assertEqual(cli.main(["show"]), 8)
        command.assert_called_once_with(("", ""), "token", "o", "c", "1")

        contest_context = {"contest": {"org": "o", "comp": "c", "title": "Contest"}}
        with patch.object(cli, "require_auth", return_value=auth), patch.object(
            cli, "load_context", return_value=contest_context
        ), patch.object(cli, "print_competitions") as command:
            self.assertEqual(cli.main(["show"]), 0)
        command.assert_called_once_with([contest_context["contest"]])

    def test_show_without_context_is_actionable(self) -> None:
        stdout = io.StringIO()
        with patch.object(cli, "require_auth", return_value=({}, ("", ""), "token")):
            with patch.object(cli, "load_context", return_value={}):
                with contextlib.redirect_stdout(stdout):
                    result = cli.main(["show"])
        self.assertEqual(result, 1)
        self.assertIn("naij use", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

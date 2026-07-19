from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nitro_ai_judge_cli import shell, state  # noqa: E402


class ShellLoopTests(unittest.TestCase):
    def run_with_input(self, side_effect: list[object], dispatch: Mock | None = None):
        dispatch = dispatch or Mock(return_value=0)
        stdout = io.StringIO()
        with patch.object(shell, "setup_readline"), patch.object(
            shell, "save_shell_history"
        ) as save_history, patch("builtins.input", side_effect=side_effect):
            with contextlib.redirect_stdout(stdout):
                result = shell.run_shell(dispatch)
        return result, stdout.getvalue(), dispatch, save_history

    def test_ctrl_c_cancels_current_line_and_shell_continues(self) -> None:
        result, output, dispatch, save_history = self.run_with_input(
            [KeyboardInterrupt(), "q"]
        )
        self.assertEqual(result, 0)
        self.assertIn("Nitro AI Judge Interactive Shell", output)
        dispatch.assert_not_called()
        save_history.assert_called_once()

    def test_ctrl_d_exits_cleanly_and_saves_history(self) -> None:
        result, _, dispatch, save_history = self.run_with_input([EOFError()])
        self.assertEqual(result, 0)
        dispatch.assert_not_called()
        save_history.assert_called_once()

    def test_invalid_quoted_input_never_terminates_shell(self) -> None:
        result, output, dispatch, _ = self.run_with_input(["'unterminated", "q"])
        self.assertEqual(result, 0)
        self.assertIn("No closing quotation", output)
        dispatch.assert_not_called()

    def test_parser_system_exit_does_not_terminate_shell(self) -> None:
        dispatch = Mock(side_effect=[SystemExit(2)])
        result, _, dispatch, _ = self.run_with_input(["not-a-command", "q"], dispatch)
        self.assertEqual(result, 0)
        dispatch.assert_called_once_with(["not-a-command"])

    def test_ctrl_c_during_dispatch_cancels_command_and_shell_continues(self) -> None:
        dispatch = Mock(side_effect=[KeyboardInterrupt()])
        result, _, dispatch, _ = self.run_with_input(["ls", "q"], dispatch)
        self.assertEqual(result, 0)
        dispatch.assert_called_once_with(["ls"])

    def test_help_command_dispatches_canonical_command_help(self) -> None:
        result, _, dispatch, _ = self.run_with_input(["help submit", "q"])
        self.assertEqual(result, 0)
        dispatch.assert_called_once_with(["submit", "--help"])


class ShellContextTests(unittest.TestCase):
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

    def test_prompt_restores_persistent_contest_and_task(self) -> None:
        state.set_contest({"organizationSlug": "org", "competitionSlug": "contest"})
        state.set_task({"id": "task"})
        self.assertEqual(shell.shell_prompt(), "[naij org/contest task] > ")

    def test_back_moves_up_one_persistent_context_level(self) -> None:
        state.set_contest({"organizationSlug": "org", "competitionSlug": "contest"})
        state.set_task({"id": "task"})
        state.set_submission("submission")

        shell._back()
        self.assertIsNone(state.selected_submission())
        self.assertEqual(state.selected_task(), "task")
        shell._back()
        self.assertIsNone(state.selected_task())
        self.assertEqual(state.selected_contest(), ("org", "contest"))
        shell._back()
        self.assertIsNone(state.selected_contest())

    def test_bare_numbers_navigate_cached_contests_tasks_and_submissions(self) -> None:
        state.save_context(
            {
                "cache": {
                    "contests": {
                        "all": [
                            {"organizationSlug": "org", "competitionSlug": "contest"}
                        ]
                    },
                    "tasks": {"org/contest": [{"id": "task"}]},
                    "submissions": {
                        "org/contest/task": [{"id": "submission-3"}]
                    },
                }
            }
        )

        self.assertTrue(shell._numeric_select("1"))
        self.assertEqual(state.selected_contest(), ("org", "contest"))
        self.assertTrue(shell._numeric_select("1"))
        self.assertEqual(state.selected_task(), "task")
        self.assertTrue(shell._numeric_select("3"))
        self.assertEqual(state.selected_submission(), "submission-3")

    def test_invalid_numeric_selection_leaves_context_unchanged(self) -> None:
        state.save_context({"cache": {"contests": {"all": []}}})
        before = state.load_context()
        self.assertFalse(shell._numeric_select("9"))
        self.assertEqual(state.load_context(), before)

    def test_completed_entities_select_cached_context_case_insensitively(self) -> None:
        state.save_context(
            {
                "cache": {
                    "contests": {
                        "all": [
                            {"organizationSlug": "CeoAI", "competitionSlug": "Open"}
                        ]
                    },
                    "tasks": {"CeoAI/Open": [{"id": "Forecast"}]},
                    "submissions": {
                        "CeoAI/Open/Forecast": [{"id": "submission-ABC123"}]
                    },
                }
            }
        )

        self.assertTrue(shell._entity_select("ceoai/open"))
        self.assertTrue(shell._entity_select("forecast"))
        self.assertTrue(shell._entity_select("abc123"))
        self.assertEqual(state.selected_submission(), "submission-ABC123")


class ReadlineTests(unittest.TestCase):
    def test_readline_wires_forward_backward_and_slash_aware_completion(self) -> None:
        completer_set = Mock()
        bindings: list[str] = []

        with tempfile.TemporaryDirectory() as directory:
            history = str(Path(directory) / "history")
            Path(history).write_text("", encoding="utf-8")
            with patch.object(shell, "prepare_history", return_value=history), patch.object(
                shell.readline, "read_history_file"
            ), patch.object(
                shell.readline, "get_completer_delims", return_value=" \t/"
            ), patch.object(
                shell.readline, "set_completer_delims"
            ) as delimiters, patch.object(
                shell.readline, "set_completer", completer_set
            ), patch.object(
                shell.readline, "parse_and_bind", side_effect=bindings.append
            ), patch.object(
                shell.readline, "get_line_buffer", return_value="use Acme/"
            ), patch.object(
                shell.readline, "get_begidx", return_value=len("use ")
            ), patch.object(
                shell, "candidates", return_value=["Acme/Open"]
            ) as candidates:
                shell.setup_readline()

                completer = completer_set.call_args.args[0]
                self.assertEqual(completer("Acme/", 0), "Acme/Open")
                self.assertIsNone(completer("Acme/", 1))

        self.assertNotIn("/", delimiters.call_args.args[0])
        self.assertIn("tab: menu-complete", bindings)
        self.assertIn('"\\e[Z": menu-complete-backward', bindings)
        candidates.assert_called_once_with(["use", "Acme/"], interactive=True)


if __name__ == "__main__":
    unittest.main()

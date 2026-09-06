from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from nitro_ai_judge_cli import cli, play
from nitro_ai_judge_cli.play_manager_client import ManagerClient


class DetachedPlayTests(unittest.TestCase):
    def test_perform_detach_queues_without_waiting(self) -> None:
        client = Mock()
        client.action.return_value = {"operation_id": "op-detached"}
        output = io.StringIO()

        with patch.object(play, "_client", return_value=client) as get_client, contextlib.redirect_stdout(output):
            result = play.perform_play_action("org", "contest", "play", detach=True)

        self.assertEqual(result, {"operation_id": "op-detached", "detached": True})
        get_client.assert_called_once_with(yes=False)
        client.action.assert_called_once_with("org", "contest", "play")
        client.wait_operation.assert_not_called()
        self.assertIn("Operation queued: op-detached", output.getvalue())

    def test_cli_detach_does_not_wait_open_browser_or_report_completion(self) -> None:
        client = Mock()
        client.action.return_value = {"operation_id": "op-detached"}
        output = io.StringIO()

        with (
            patch.object(cli, "load_context", return_value={}),
            patch.object(play, "_client", return_value=client) as get_client,
            patch.object(play.webbrowser, "open") as open_browser,
            contextlib.redirect_stdout(output),
        ):
            result = cli.main(["play", "play", "org/contest", "--detach"])

        self.assertEqual(result, 0)
        get_client.assert_called_once_with(yes=False)
        client.wait_operation.assert_not_called()
        open_browser.assert_not_called()
        self.assertNotIn("Play play complete", output.getvalue())


class ContextFreePlayListingTests(unittest.TestCase):
    def test_cli_ls_uses_manager_state_without_loading_context(self) -> None:
        client = Mock()
        client.info.return_value = {"identity": "naij-play-manager", "api_version": 1}
        client.competitions.return_value = []
        output = io.StringIO()

        with (
            patch.object(cli, "load_context", side_effect=AssertionError("context")),
            patch.object(ManagerClient, "from_state", return_value=client) as from_state,
            patch.object(play, "_client", side_effect=AssertionError("_client")),
            contextlib.redirect_stdout(output),
        ):
            result = cli.main(["play", "ls"])

        self.assertEqual(result, 0)
        from_state.assert_called_once_with()
        client.competitions.assert_called_once_with()
        self.assertIn("No managed Play environments", output.getvalue())

    def test_cli_operations_uses_manager_state_without_loading_context(self) -> None:
        client = Mock()
        client.info.return_value = {"identity": "naij-play-manager", "api_version": 1}
        client.operations.return_value = []
        output = io.StringIO()

        with (
            patch.object(cli, "load_context", side_effect=AssertionError("context")),
            patch.object(ManagerClient, "from_state", return_value=client) as from_state,
            patch.object(play, "_client", side_effect=AssertionError("_client")),
            contextlib.redirect_stdout(output),
        ):
            result = cli.main(["play", "operations", "--limit", "7"])

        self.assertEqual(result, 0)
        from_state.assert_called_once_with()
        client.operations.assert_called_once_with(limit=7)
        self.assertIn("No Play operations", output.getvalue())


class PlayArgumentValidationTests(unittest.TestCase):
    def test_parser_rejects_detach_with_open(self) -> None:
        parser = cli.build_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["play", "play", "org/contest", "--detach", "--open"])

    def test_parser_rejects_detach_for_readonly_actions(self) -> None:
        parser = cli.build_parser()
        for action in ("status", "ps", "cancel", "open", "logs"):
            with self.subTest(action=action), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(["play", action, "org/contest", "--detach"])

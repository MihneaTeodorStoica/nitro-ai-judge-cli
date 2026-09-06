from __future__ import annotations

import asyncio
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button
from tests import test_tui as fixtures
from tests.test_tui import TASKS, FakeManager
from nitro_ai_judge_cli import cli, tui
from nitro_ai_judge_cli.tui_paths import complete_path


class PathAndOutputTests(unittest.TestCase):
    def test_path_completion_preserves_spaces_and_tilde(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"HOME": root, "USERPROFILE": root}):
            Path(root, "my folder").mkdir()
            Path(root, "answer.csv").touch()
            self.assertEqual(complete_path("~/my"), "~/my folder" + os.sep)
            self.assertEqual(complete_path("~/ans"), "~/answer.csv")
            self.assertIsNone(complete_path("~/ans", directories_only=True))
            self.assertIsNone(complete_path("~/missing/path"))

    def test_stdout_auth_failure_does_not_contaminate_pipe(self):
        def auth():
            print("Please log in")
            return None
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "require_auth", side_effect=auth), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["download-data", "-c", "statement", "-o", "-"])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("log in", err.getvalue())

    def test_legacy_complete_submission_label_uses_complete_score(self):
        label = tui.submission_label({"id": "x", "state": "finished", "completeTaskScore": 88})
        self.assertIn("88", label)
        self.assertNotIn("In Queue", label)


class TUILifecycleTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.TUIPilotTests.setUp
    tearDown = fixtures.TUIPilotTests.tearDown
    auth_patches = fixtures.TUIPilotTests.auth_patches
    cache_selection = fixtures.TUIPilotTests.cache_selection

    async def test_worker_cancellation_finishes_dom_update(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def dom_update():
            entered.set()
            await release.wait()
            finished.set()

        worker = asyncio.create_task(tui._finish_dom_update(dom_update()))
        await entered.wait()
        worker.cancel()
        await asyncio.sleep(0)
        self.assertFalse(worker.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertTrue(finished.is_set())

    async def test_final_confirm_cancel_success_failure(self):
        self.cache_selection()
        with self.auth_patches(tasks=TASKS), patch.object(tui, "set_submission_final") as update:
            app = tui.NitroTUI(manager_client=FakeManager())
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.2)
                app.current_submission = {"id": "full-id", "state": "finished", "isFinal": False}
                app.action_view(3)
                await pilot.pause()
                await pilot.press("f")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.ConfirmScreen)
                update.assert_not_called()
                await pilot.press("n")
                await pilot.pause()
                self.assertFalse(app.current_submission["isFinal"])
                await pilot.press("f")
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause(0.2)
                self.assertTrue(app.current_submission["isFinal"])
                self.assertEqual(update.call_args.args[-2:], ("full-id", True))
                self.assertEqual(str(app.query_one("#submission-final", Button).label), "Unset final")
                update.side_effect = RuntimeError("server refused")
                await pilot.click("#submission-final")
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause(0.2)
                self.assertTrue(app.current_submission["isFinal"])
                self.assertIn("server refused", str(app.query_one("#status-line").content))

    async def test_pending_poller_deduplicates_and_stops_on_view_exit(self):
        self.cache_selection()
        pending = {"id": "waiting-id", "state": "pending", "username": "tester"}
        with self.auth_patches(tasks=TASKS), patch.object(tui, "load_submission", return_value=pending) as load, patch.object(tui, "SUBMISSION_POLL_INTERVAL", 0.03):
            app = tui.NitroTUI(manager_client=FakeManager())
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.2)
                app.current_submission = dict(pending)
                app.action_view(3)
                await pilot.pause(0.1)
                worker = app._submission_poller
                app._resume_pending_submission_poll_current()
                self.assertIs(app._submission_poller, worker)
                self.assertGreaterEqual(load.call_count, 2)
                app.action_view(1)
                await pilot.pause(0.1)
                count = load.call_count
                await pilot.pause(0.1)
                self.assertEqual(load.call_count, count)
                self.assertTrue(worker.is_finished)

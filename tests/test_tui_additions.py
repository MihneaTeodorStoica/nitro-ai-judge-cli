from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from textual.widgets import Input, ListView, Static

import test_tui as _tui_tests

CONTEST = _tui_tests.CONTEST
TASKS = _tui_tests.TASKS
state = _tui_tests.state
tui = _tui_tests.tui


class TUIPilotAdditionsTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the existing Pilot fixture without inheriting its complete test suite.
    setUp = _tui_tests.TUIPilotTests.setUp
    tearDown = _tui_tests.TUIPilotTests.tearDown
    auth_patches = _tui_tests.TUIPilotTests.auth_patches
    cache_selection = _tui_tests.TUIPilotTests.cache_selection

    async def test_proxy_submission_requires_source_before_calling_api(self) -> None:
        self.cache_selection()
        with (
            self.auth_patches(contests=[CONTEST], tasks=TASKS),
            patch.object(
                tui, "runtime", return_value=SimpleNamespace(submission_proxy=True)
            ),
            patch.object(
                tui, "create_submission", return_value={"submissionID": "new"}
            ) as create,
            patch.object(
                tui, "load_submission", return_value={"id": "new", "state": "finished"}),
            patch.object(tui, "SUBMISSION_POLL_INTERVAL", 0.01),
        ):
            app = tui.NitroTUI()
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.2)
                await pilot.press("s")
                await pilot.pause()
                app.screen.query_one("#submit-output", Input).value = "answer.csv"
                await pilot.press("enter", "enter", "enter")
                await pilot.pause()

                self.assertIsInstance(app.screen, tui.SubmitScreen)
                self.assertIn(
                    "Enter a source file",
                    str(app.screen.query_one("#submit-error", Static).content),
                )
                create.assert_not_called()

                app.screen.query_one("#submit-source", Input).value = "solution.py"
                await pilot.press("enter", "enter")
                await pilot.pause(0.2)

                create.assert_called_once()
                self.assertEqual(create.call_args.args[5:7], ("answer.csv", "solution.py"))

    async def test_download_warning_is_visible_in_tui_status(self) -> None:
        self.cache_selection()
        with (
            self.auth_patches(contests=[CONTEST], tasks=TASKS),
            patch.object(
                tui,
                "download_task_data",
                return_value=[{"category": "data", "warning": "checksum skipped"}],
            ) as download,
        ):
            app = tui.NitroTUI()
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.2)
                await app.perform_download(
                    tui.DownloadRequest(["data"], str(self.root), None)
                )
                self.assertIn(
                    "checksum skipped",
                    str(app.query_one("#status-line", Static).content),
                )
                download.assert_called_once()

    async def test_offline_submission_rows_and_selected_detail_survive_rerender(self) -> None:
        self.cache_selection()
        cached = {
            "id": "offline-submission",
            "username": "tester",
            "state": "finished",
            "verdictMessage": "Cached verdict",
        }
        state.update_cache("submissions", "org/contest/backend-7", [cached])
        state.set_submission(cached)
        with (
            self.auth_patches(contests=[CONTEST], tasks=TASKS),
            patch.object(tui, "load_submissions", side_effect=RuntimeError("offline")),
        ):
            app = tui.NitroTUI()
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.2)
                await pilot.press("3")
                await pilot.pause(0.2)
                rows = app.query_one("#submission-list", ListView)
                self.assertEqual(len(rows.children), 1)
                self.assertEqual(app.current_submission["id"], "offline-submission")

                app.current_submission = {**cached, "verdictMessage": "Selected detail"}
                await app.render_submissions()

                self.assertEqual(len(rows.children), 1)
                self.assertEqual(app.current_submission["verdictMessage"], "Selected detail")
                self.assertIn(
                    "Selected detail",
                    str(app.query_one("#submission-detail", Static).content),
                )


if __name__ == "__main__":
    unittest.main()

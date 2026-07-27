from __future__ import annotations

import asyncio
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from textual.widgets import (  # noqa: E402
    ContentSwitcher,
    Input,
    ListView,
    Static,
    Tabs,
)

from nitro_ai_judge_cli import cli, state, tui  # noqa: E402


AUTH_STATE = {
    "access_token": "token",
    "refresh_token": "refresh",
    "username": "tester",
}
CONTEST = {
    "organizationSlug": "org",
    "competitionSlug": "contest",
    "title": "Contest One",
}
OTHER_CONTEST = {
    "organizationSlug": "other",
    "competitionSlug": "second",
    "title": "Second Contest",
}
TASKS = [
    {"id": "backend-7", "title": "First Task", "synopsis": "First synopsis"},
    {"id": "backend-9", "title": "Second Task", "synopsis": "Second synopsis"},
]
PLAY_STATUS = {
    "reference": "org/contest",
    "workspace_state": "running",
    "service_health": "healthy",
    "jupyter_url": "/nitro/competitions/org/contest/jupyter/",
    "proxy_url": "/nitro/competitions/org/contest/proxy/",
    "gpu": "enabled",
    "images": {
        "notebook": {"name": "notebook", "state": "ready"},
        "proxy": {"name": "proxy", "state": "ready"},
    },
    "workspace": "workspace",
}


class FakeManager:
    base_url = "http://localhost:51123"

    def __init__(self) -> None:
        self.actions: list[str] = []

    def competition(self, org: str, competition: str) -> dict:
        return dict(PLAY_STATUS)

    def logs(self, org: str, competition: str, *, tail: int = 80) -> dict:
        return {"logs": "recent log"}

    def action(self, org: str, competition: str, action: str, **options: object) -> dict:
        self.actions.append(action)
        return {"operation_id": f"operation-{action}"}

    def wait_operation(self, operation_id: str, *, timeout: float = 600) -> dict:
        return {"status": "complete", "result": dict(PLAY_STATUS)}

    def open_info(self, org: str, competition: str) -> dict:
        return {
            "jupyter_url": "http://localhost:51123/nitro/competitions/org/contest/jupyter/"
        }


def invoke(argv: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        try:
            result = cli.main(argv)
        except SystemExit as exc:
            result = int(exc.code or 0)
    return int(result), output.getvalue()


class TUIEntrypointTests(unittest.TestCase):
    def test_ready_workspace_offers_play_and_recreate_without_start(self) -> None:
        actions = [action for action, _ in tui.PlayMenu("ready").actions]
        self.assertEqual(actions[:2], ["play", "recreate"])
        self.assertNotIn("start", actions)

    def test_tui_help_dispatch_and_mouse_enabled_runtime(self) -> None:
        result, output = invoke(["tui", "--help"])
        self.assertEqual(result, 0)
        self.assertIn("usage: naij tui", output)

        with (
            patch.object(cli, "require_auth", side_effect=AssertionError("auth")),
            patch.object(tui, "run_tui", return_value=23) as run,
        ):
            self.assertEqual(cli.main(["tui"]), 23)
        run.assert_called_once_with()

        app = unittest.mock.Mock()
        app.run.return_value = 0
        with patch.object(tui, "NitroTUI", return_value=app):
            self.assertEqual(tui.run_tui(), 0)
        app.run.assert_called_once_with(mouse=True)


class TUIAuthSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_regenerates_bearer_and_cookie_then_retries_once(
        self,
    ) -> None:
        old = {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "username": "tester",
        }
        new = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "username": "tester",
        }
        session = tui.TUIAuthSession(old)
        old_cookie = session.cookies[1]
        seen: list[tuple[tuple[str, str], str]] = []

        def request(cookies: tuple[str, str], bearer: str) -> str:
            seen.append((cookies, bearer))
            if bearer == "old-access":
                raise RuntimeError("HTTP 401: expired")
            return "ok"

        with patch.object(tui, "refresh_saved_tokens", return_value=new) as refresh:
            self.assertEqual(await session.call(request), "ok")

        refresh.assert_called_once_with(old)
        self.assertEqual([bearer for _, bearer in seen], ["old-access", "new-access"])
        self.assertNotEqual(seen[1][0][1], old_cookie)
        self.assertEqual(session.bearer, "new-access")

    async def test_failed_retry_opens_login_without_a_loop(self) -> None:
        old = {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
        }
        new = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        session = tui.TUIAuthSession(old)
        calls = 0

        def denied(_cookies: tuple[str, str], _bearer: str) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("HTTP 401: expired")

        with patch.object(tui, "refresh_saved_tokens", return_value=new) as refresh:
            with self.assertRaises(tui.LoginRequired):
                await session.call(denied)
        self.assertEqual(calls, 2)
        refresh.assert_called_once()

    async def test_post_refresh_403_remains_access_denied(self) -> None:
        old = {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
        }
        new = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        session = tui.TUIAuthSession(old)

        def request(_cookies: tuple[str, str], bearer: str) -> None:
            if bearer == "old-access":
                raise RuntimeError("HTTP 401: expired")
            raise RuntimeError("HTTP 403: forbidden")

        with patch.object(tui, "refresh_saved_tokens", return_value=new):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                await session.call(request)

    async def test_concurrent_failures_share_one_refresh(self) -> None:
        old = {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
        }
        new = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        session = tui.TUIAuthSession(old)
        barrier = threading.Barrier(2)

        def request(_cookies: tuple[str, str], bearer: str) -> str:
            if bearer == "old-access":
                barrier.wait(timeout=2)
                raise RuntimeError("HTTP 401: expired")
            return bearer

        with patch.object(tui, "refresh_saved_tokens", return_value=new) as refresh:
            results = await asyncio.gather(
                session.call(request),
                session.call(request),
            )
        self.assertEqual(results, ["new-access", "new-access"])
        refresh.assert_called_once()


class TUIPilotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "NAIJ_STATE_DIR": str(self.root / "state"),
            },
            clear=True,
        )
        self.environment.start()
        state.configure_state_dir(None)
        state.reset_state_paths()
        state._warned.clear()

    def tearDown(self) -> None:
        state.configure_state_dir(None)
        state.reset_state_paths()
        state._warned.clear()
        self.environment.stop()
        self.temporary.cleanup()

    def auth_patches(
        self,
        *,
        contests: list[dict] | None = None,
        tasks: list[dict] | None = None,
        categories: list[str] | None = None,
    ) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(tui, "load_state", return_value=AUTH_STATE))
        stack.enter_context(
            patch.object(tui, "ensure_fresh_state", return_value=AUTH_STATE)
        )
        stack.enter_context(
            patch.object(
                tui,
                "get_auth",
                return_value=("clearance", "site-cookie", "token"),
            )
        )
        stack.enter_context(
            patch.object(tui, "load_competitions", return_value=list(contests or []))
        )
        stack.enter_context(
            patch.object(tui, "load_tasks", return_value=list(tasks or []))
        )
        stack.enter_context(
            patch.object(
                tui,
                "load_task_view",
                side_effect=lambda _cookies, _bearer, _org, _comp, task_id: {
                    "task": next(
                        (
                            task
                            for task in (tasks or [])
                            if str(task.get("id")) == str(task_id)
                        ),
                        {"id": task_id, "title": str(task_id)},
                    )
                },
            )
        )
        stack.enter_context(
            patch.object(
                tui,
                "load_task_file_categories",
                return_value=list(categories or []),
            )
        )
        stack.enter_context(
            patch.object(tui, "load_submissions", return_value=([], 1))
        )
        stack.enter_context(
            patch.object(tui.ManagerClient, "from_state", return_value=FakeManager())
        )
        return stack

    def cache_selection(self) -> None:
        state.update_cache("contests", "all", [CONTEST, OTHER_CONTEST])
        state.update_cache("tasks", "org/contest", TASKS)
        state.set_contest(CONTEST)
        state.set_task(TASKS[0])

    async def test_login_failure_then_success_is_keyboard_only(self) -> None:
        denied = {"success": False, "tokens": None, "error": "denied"}
        accepted = {
            "success": True,
            "tokens": {"access_token": "new-token"},
            "username": "tester",
        }
        with (
            patch.object(tui, "load_state", side_effect=[None, AUTH_STATE]),
            patch.object(
                tui,
                "get_auth",
                return_value=("clearance", "new-cookie", "new-token"),
            ),
            patch.object(tui, "do_login", side_effect=[denied, accepted]),
            patch.object(tui, "save_token_state"),
            patch.object(tui, "load_competitions", return_value=[]),
        ):
            app = tui.NitroTUI()
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, tui.LoginScreen)
                login_status = app.screen.query_one("#login-error", Static)
                login_status.update("Signing in…")
                self.assertEqual(
                    login_status.content_region.x,
                    app.screen.query_one("#login-password", Input).content_region.x,
                )
                login_status.update("")
                app.screen.query_one("#login-username", Input).value = "tester"
                await pilot.press("enter")
                app.screen.query_one("#login-password", Input).value = "secret"
                await pilot.press("enter")
                await pilot.pause(0.1)
                self.assertIn(
                    "denied",
                    str(app.screen.query_one("#login-error", Static).content),
                )

                app.screen.query_one("#login-password", Input).value = "secret"
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertNotIsInstance(app.screen, tui.LoginScreen)
                self.assertEqual(app.session.bearer, "new-token")
                self.assertIn("tester", str(app.query_one("#header-line").content))

    async def test_filter_navigation_views_help_refresh_and_quit(self) -> None:
        state.update_cache("contests", "all", [CONTEST, OTHER_CONTEST])
        with self.auth_patches(
            contests=[CONTEST, OTHER_CONTEST],
            tasks=TASKS,
            categories=["statement"],
        ) as stack:
            load_tasks = stack.enter_context(
                patch.object(tui, "load_tasks", return_value=TASKS)
            )
            app = tui.NitroTUI()
            async with app.run_test(size=(120, 34)) as pilot:
                await pilot.pause(0.2)
                contests = app.query_one("#contest-list", ListView)
                tasks = app.query_one("#task-list", ListView)
                self.assertEqual(len(contests.children), 2)

                await pilot.press("/")
                app.query_one("#contest-filter", Input).value = "Second"
                await pilot.pause(0.1)
                self.assertEqual(len(contests.children), 1)
                await pilot.press("escape")
                await pilot.pause(0.1)
                self.assertEqual(len(contests.children), 2)

                contests.index = 0
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertEqual(app.active_pane, "tasks")
                labels = [
                    str(item.query_one("Label").content) for item in tasks.children
                ]
                self.assertEqual(labels, ["1. First Task\nFirst synopsis", "2. Second Task\nSecond synopsis"])

                await pilot.press("j", "enter")
                await pilot.pause(0.2)
                self.assertEqual(app.current_task["id"], "backend-9")
                self.assertEqual(state.selected_task(), "backend-9")
                self.assertEqual(app.active_pane, "right")

                await pilot.press("left", "left", "right")
                await pilot.pause(0.1)
                self.assertEqual(app.active_pane, "tasks")
                self.assertEqual(tasks.index, 1)
                self.assertEqual(app.current_task["id"], "backend-9")
                await pilot.press("right")

                for number, view_id in (
                    ("2", "view-data"),
                    ("3", "view-submissions"),
                    ("4", "view-play"),
                    ("1", "view-overview"),
                ):
                    await pilot.press(number)
                    await pilot.pause(0.05)
                    self.assertEqual(
                        app.query_one("#task-views", ContentSwitcher).current,
                        view_id,
                    )

                await pilot.press("?")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.HelpScreen)
                await pilot.press("?")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, tui.HelpScreen)

                await pilot.press("left", "r")
                await pilot.pause(0.1)
                self.assertGreaterEqual(load_tasks.call_count, 2)
                await pilot.press("q")
            self.assertTrue(app.return_value is None or app.return_value == 0)

    async def test_network_failure_preserves_cached_list(self) -> None:
        state.update_cache("contests", "all", [CONTEST])
        with self.auth_patches(contests=[CONTEST]) as stack:
            stack.enter_context(
                patch.object(
                    tui,
                    "load_competitions",
                    side_effect=RuntimeError("offline"),
                )
            )
            app = tui.NitroTUI()
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.2)
                self.assertEqual(
                    len(app.query_one("#contest-list", ListView).children),
                    1,
                )
                self.assertIn(
                    "Cached data remains available",
                    str(app.query_one("#status-line", Static).content),
                )

    async def test_mouse_selection_augments_the_keyboard_flow(self) -> None:
        state.update_cache("contests", "all", [CONTEST, OTHER_CONTEST])
        with self.auth_patches(
            contests=[CONTEST, OTHER_CONTEST],
            tasks=TASKS,
        ):
            app = tui.NitroTUI()
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(0.2)
                contests = app.query_one("#contest-list", ListView)
                first_contest_item = contests.children[0]
                self.assertTrue(await pilot.click(contests.children[0]))
                await pilot.pause(0.2)
                tasks = app.query_one("#task-list", ListView)
                self.assertIs(contests.children[0], first_contest_item)
                self.assertTrue(app.query_one("#contest-pane").display)
                self.assertTrue(app.query_one("#task-pane").display)
                self.assertEqual(app.current_contest, CONTEST)
                self.assertEqual(app.active_pane, "tasks")
                self.assertTrue(await pilot.click(tasks.children[1]))
                await pilot.pause(0.2)
                self.assertEqual(app.current_task["id"], "backend-9")

                for tab_id, number, view_id in (
                    ("tab-data", 2, "view-data"),
                    ("tab-submissions", 3, "view-submissions"),
                    ("tab-play", 4, "view-play"),
                    ("tab-overview", 1, "view-overview"),
                ):
                    self.assertTrue(await pilot.click(f"#{tab_id}"))
                    await pilot.pause(0.05)
                    self.assertEqual(app.active_view, number)
                    self.assertEqual(
                        app.query_one("#task-views", ContentSwitcher).current,
                        view_id,
                    )

                await pilot.press("l", "right", "h", "left")
                await pilot.pause(0.1)
                self.assertEqual(app.active_view, 1)
                self.assertEqual(
                    app.query_one("#view-nav", Tabs).active,
                    "tab-overview",
                )

    async def test_long_task_statement_scrolls_in_the_right_pane(self) -> None:
        long_task = {
            **TASKS[0],
            "statement": "# First Task\n\n"
            + "\n\n".join(f"Paragraph {number}" for number in range(80)),
        }
        state.update_cache("contests", "all", [CONTEST])
        state.update_cache("tasks", "org/contest", [long_task])
        state.set_contest(CONTEST)
        state.set_task(long_task)
        with self.auth_patches(contests=[CONTEST], tasks=[long_task]):
            app = tui.NitroTUI()
            async with app.run_test(size=(100, 24)) as pilot:
                await pilot.pause(0.3)
                await pilot.press("1")
                await pilot.pause(0.2)
                await pilot.press("j", "j", "j")
                await pilot.pause(0.1)
                overview = app.query_one("#view-overview")
                self.assertGreater(overview.scroll_y, 0)
                await pilot.press("k", "k", "k")
                await pilot.pause(0.1)
                self.assertEqual(overview.scroll_y, 0)

    async def test_loaded_statement_is_cached_with_the_task(self) -> None:
        self.cache_selection()
        detailed = {
            **TASKS[0],
            "statement": "# Cached statement\n\nFull task details.",
        }
        with (
            self.auth_patches(contests=[CONTEST], tasks=TASKS),
            patch.object(
                tui,
                "load_task_view",
                return_value={"task": detailed},
            ),
        ):
            app = tui.NitroTUI()
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.3)

        cached = state.cached_items("tasks", "org/contest")
        self.assertEqual(cached[0]["statement"], detailed["statement"])

    async def test_download_form_and_overwrite_confirmation_use_keys(self) -> None:
        self.cache_selection()
        destination = self.root / "existing.csv"
        destination.write_text("old", encoding="utf-8")
        with (
            self.auth_patches(
                contests=[CONTEST],
                tasks=TASKS,
                categories=["test_data"],
            ),
            patch.object(
                tui,
                "download_task_data",
                return_value=[
                    {
                        "category": "test_data",
                        "path": str(destination),
                        "bytes": 4,
                    }
                ],
            ) as download,
        ):
            app = tui.NitroTUI()
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause(0.2)
                await pilot.press("d")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.DownloadScreen)
                await pilot.press("tab")
                app.screen.query_one("#download-directory", Input).value = str(
                    self.root
                )
                await pilot.press("tab")
                app.screen.query_one("#download-output", Input).value = str(
                    destination
                )
                await pilot.press("enter")
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, tui.ConfirmScreen)
                await pilot.press("y")
                await pilot.pause(0.2)

                self.assertTrue(download.call_args.kwargs["force"])
                self.assertFalse(download.call_args.kwargs["show_progress"])
                self.assertIn(
                    "Downloaded 1",
                    str(app.query_one("#status-line", Static).content),
                )

    async def test_submission_form_polls_to_terminal_feedback(self) -> None:
        self.cache_selection()
        complete = {
            "id": "new-submission",
            "state": "finished",
            "partialTaskScore": 90,
            "verdictMessage": "Accepted",
        }
        with (
            self.auth_patches(contests=[CONTEST], tasks=TASKS),
            patch.object(
                tui,
                "create_submission",
                return_value={"submissionID": "new-submission"},
            ),
            patch.object(tui, "load_submission", return_value=complete),
            patch.object(tui, "SUBMISSION_POLL_INTERVAL", 0.01),
        ):
            app = tui.NitroTUI()
            async with app.run_test(size=(60, 20)) as pilot:
                await pilot.pause(0.2)
                await pilot.press("s")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.SubmitScreen)
                app.screen.query_one("#submit-output", Input).value = "answer.csv"
                await pilot.press("enter")
                app.screen.query_one("#submit-source", Input).value = "solution.py"
                await pilot.press("enter")
                app.screen.query_one("#submit-note", Input).value = "note"
                self.assertTrue(await pilot.click("#submit-confirm"))
                await pilot.pause(0.3)

                self.assertEqual(app.current_submission["state"], "finished")
                self.assertIn(
                    "Accepted",
                    str(app.query_one("#submission-detail", Static).content),
                )
                self.assertEqual(app.active_view, 3)

    async def test_submission_form_submits_from_keyboard(self) -> None:
        self.cache_selection()
        complete = {
            "id": "keyboard-submission",
            "state": "finished",
            "verdictMessage": "Accepted",
        }
        with (
            self.auth_patches(contests=[CONTEST], tasks=TASKS),
            patch.object(
                tui,
                "create_submission",
                return_value={"submissionID": "keyboard-submission"},
            ) as create,
            patch.object(tui, "load_submission", return_value=complete),
            patch.object(tui, "SUBMISSION_POLL_INTERVAL", 0.01),
        ):
            app = tui.NitroTUI()
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.2)
                await pilot.press("s")
                await pilot.pause()
                app.screen.query_one("#submit-output", Input).value = "answer.csv"
                await pilot.press("enter", "enter", "enter")
                await pilot.pause(0.3)

                self.assertNotIsInstance(app.screen, tui.SubmitScreen)
                self.assertEqual(app.current_submission["id"], "keyboard-submission")
                create.assert_called_once()

    async def test_submissions_view_is_user_only_and_has_new_button(self) -> None:
        self.cache_selection()
        own = {
            "id": "mine",
            "username": "tester",
            "state": "finished",
        }
        other = {
            "id": "theirs",
            "username": "someone-else",
            "state": "finished",
        }
        authors: list[str | None] = []

        def load_for_user(
            _cookies: tuple[str, str],
            _bearer: str,
            _org: str,
            _comp: str,
            _task_id: str,
            *,
            author: str | None,
            page: int | None,
            page_size: int,
            mode: str,
        ) -> tuple[list[dict], int]:
            authors.append(author)
            return ([own, other] if mode == "partial" else [], 1)

        with (
            self.auth_patches(contests=[CONTEST], tasks=TASKS),
            patch.object(tui, "load_submissions", side_effect=load_for_user),
        ):
            app = tui.NitroTUI()
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause(0.2)
                await pilot.press("3")
                await pilot.pause(0.2)
                rows = app.query_one("#submission-list", ListView)
                self.assertEqual(len(rows.children), 1)
                self.assertEqual(app.visible_submissions[0]["id"], "mine")
                self.assertTrue(await pilot.click("#new-submission"))
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.SubmitScreen)
                await pilot.press("escape")

        self.assertEqual(authors, ["tester", "tester"])

    async def test_every_play_action_and_down_confirmation_use_keys(self) -> None:
        state.update_cache("contests", "all", [CONTEST])
        state.set_contest(CONTEST)
        manager = FakeManager()
        with (
            self.auth_patches(contests=[CONTEST]),
            patch.object(tui.ManagerClient, "from_state", return_value=manager),
            patch.object(tui.webbrowser, "open", return_value=True) as opened,
        ):
            app = tui.NitroTUI(manager_client=manager)
            async with app.run_test(size=(120, 34)) as pilot:
                await pilot.pause(0.2)
                await pilot.press("4")
                await pilot.pause(0.1)
                play = str(app.query_one("#play-content", Static).content)
                self.assertIn("Connections", play)
                self.assertIn("Recent logs", play)

                async def choose(index: int, *, confirm: bool = False) -> None:
                    await pilot.press("p")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, tui.PlayMenu)
                    if index:
                        await pilot.press(*(["down"] * index))
                    await pilot.press("enter")
                    await pilot.pause(0.05)
                    if confirm:
                        self.assertIsInstance(app.screen, tui.ConfirmScreen)
                        await pilot.press("y")
                    await pilot.pause(0.15)

                await choose(0)  # open
                await choose(1)  # stop
                await choose(2)  # restart
                await choose(3)  # recreate
                await choose(4, confirm=True)  # delete containers
                await choose(5)  # logs
                await choose(6)  # dashboard

                self.assertEqual(
                    manager.actions,
                    ["stop", "restart", "recreate", "delete-container"],
                )
                self.assertEqual(opened.call_count, 2)

    async def test_delete_image_menu_defaults_to_cancel(self) -> None:
        state.update_cache("contests", "all", [CONTEST])
        state.set_contest(CONTEST)
        manager = FakeManager()
        manager.competition = lambda _org, _comp: {
            **PLAY_STATUS,
            "workspace_state": "ready",
            "image_state": "ready",
        }
        with (
            self.auth_patches(contests=[CONTEST]),
            patch.object(tui.ManagerClient, "from_state", return_value=manager),
        ):
            app = tui.NitroTUI(manager_client=manager)
            async with app.run_test(size=(120, 34)) as pilot:
                await pilot.pause(0.2)
                await pilot.press("4")
                await pilot.pause(0.1)
                await pilot.press("p")
                await pilot.pause()
                menu = app.screen
                self.assertIsInstance(menu, tui.PlayMenu)
                actions = [action for action, _ in menu.actions]
                index = actions.index("delete-image")
                await pilot.press(*(["down"] * index), "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.ConfirmScreen)
                await pilot.press("enter")
                await pilot.pause(0.2)

        self.assertEqual(manager.actions, [])

    async def test_ctrl_d_quits(self) -> None:
        with self.auth_patches():
            app = tui.NitroTUI()
            async with app.run_test(size=(100, 24)) as pilot:
                await pilot.pause()
                await pilot.press("ctrl+d")
            self.assertTrue(app.return_value is None or app.return_value == 0)

    async def test_responsive_breakpoints_and_no_color_theme(self) -> None:
        with self.auth_patches():
            for size, mode in (
                ((120, 30), "wide"),
                ((80, 24), "compact"),
                ((60, 20), "compact"),
                ((59, 20), "too-small"),
                ((60, 19), "too-small"),
            ):
                app = tui.NitroTUI()
                async with app.run_test(size=size) as pilot:
                    await pilot.pause(0.1)
                    self.assertEqual(app.layout_mode, mode)
                    if mode == "wide":
                        self.assertTrue(app.query_one("#contest-pane").display)
                        self.assertTrue(app.query_one("#task-pane").display)
                        self.assertTrue(app.query_one("#right-pane").display)
                    elif mode == "compact":
                        self.assertTrue(app.query_one("#contest-pane").display)
                        self.assertFalse(app.query_one("#task-pane").display)
                        self.assertFalse(app.query_one("#right-pane").display)
                        await pilot.press("tab")
                        await pilot.pause()
                        self.assertFalse(app.query_one("#contest-pane").display)
                        self.assertTrue(app.query_one("#task-pane").display)
                        self.assertFalse(app.query_one("#right-pane").display)
                        await pilot.press("tab")
                        await pilot.pause()
                        self.assertFalse(app.query_one("#contest-pane").display)
                        self.assertFalse(app.query_one("#task-pane").display)
                        self.assertTrue(app.query_one("#right-pane").display)
                    else:
                        self.assertTrue(app.query_one("#too-small").display)

            with patch.dict(os.environ, {"NO_COLOR": "1"}):
                app = tui.NitroTUI()
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    self.assertEqual(app.theme, "naij-mono")


if __name__ == "__main__":
    unittest.main()

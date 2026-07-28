from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nitro_ai_judge_cli import contests  # noqa: E402


COOKIES = ("clearance", "session")
BEARER = "access-token"


class ContestRequestTests(unittest.TestCase):
    def test_competitions_use_api_first_with_exact_params(self) -> None:
        response = {"competitions": [{"competitionSlug": "one"}], "lastPage": "3"}
        with (
            patch.object(
                contests, "api_request_text", return_value=(200, json.dumps(response), {})
            ) as api_request,
            patch.object(contests, "request_text") as site_request,
        ):
            items, last_page = contests.load_competitions_page(
                COOKIES, BEARER, page=2, page_size=25, featured=True
            )

        self.assertEqual(items, response["competitions"])
        self.assertEqual(last_page, 3)
        api_request.assert_called_once_with(
            path="/competitions",
            bearer=BEARER,
            params={"page": 2, "page_size": 25, "featured": "true"},
        )
        site_request.assert_not_called()

    def test_competitions_fall_back_to_site_after_api_failure(self) -> None:
        events: list[str] = []
        fallback = {
            "routes/competitions/index": {
                "data": {"items": [{"competitionSlug": "fallback"}], "lastPage": 2}
            }
        }

        def api_request(**kwargs: object) -> tuple[int, str, dict[str, str]]:
            events.append("api")
            return 404, "missing", {}

        def site_request(**kwargs: object) -> tuple[int, str, dict[str, str]]:
            events.append("site")
            return 200, json.dumps(fallback), {}

        with (
            patch.object(contests, "api_request_text", side_effect=api_request) as api_mock,
            patch.object(contests, "request_text", side_effect=site_request) as site_mock,
        ):
            items, last_page = contests.load_competitions_page(
                COOKIES, BEARER, page=1, page_size=20, featured=False
            )

        self.assertEqual(events, ["api", "site"])
        self.assertEqual(items, [{"competitionSlug": "fallback"}])
        self.assertEqual(last_page, 2)
        self.assertEqual(api_mock.call_args.kwargs["path"], "/competitions")
        site_mock.assert_called_once_with(
            path="/competitions.data",
            cookies=COOKIES,
            params={"page": 1, "page_size": 20, "featured": "false"},
        )

    def test_all_competition_pages_are_returned_in_page_order(self) -> None:
        def page(
            cookies: tuple[str, str],
            bearer: str,
            *,
            page: int,
            page_size: int,
            featured: bool | None,
        ) -> tuple[list[dict[str, int]], int]:
            return [{"page": page}], 3

        with patch.object(contests, "load_competitions_page", side_effect=page):
            items = contests.load_competitions(
                COOKIES,
                BEARER,
                page=None,
                page_size=20,
                featured=True,
                all_pages=True,
            )
        self.assertEqual(items, [{"page": 1}, {"page": 2}, {"page": 3}])

    def test_competition_pagination_is_capped_and_stops_on_repeated_pages(self) -> None:
        requested: list[int] = []

        def unique_page(*args: object, **kwargs: object) -> tuple[list[dict[str, int]], int]:
            page = int(kwargs["page"])
            requested.append(page)
            return [{"page": page}], 10**9

        with (
            patch.object(contests, "MAX_PAGINATION_PAGES", 4),
            patch.object(contests, "load_competitions_page", side_effect=unique_page),
        ):
            items = contests.load_competitions(
                COOKIES, BEARER, page=None, page_size=20, featured=None
            )
        self.assertEqual(requested, [1, 2, 3, 4])
        self.assertEqual(len(items), 4)

        requested.clear()

        def repeated_page(*args: object, **kwargs: object) -> tuple[list[dict[str, int]], int]:
            page = int(kwargs["page"])
            requested.append(page)
            return [{"page": min(page, 2)}], 10**9

        with patch.object(
            contests, "load_competitions_page", side_effect=repeated_page
        ):
            contests.load_competitions(
                COOKIES, BEARER, page=None, page_size=20, featured=None
            )
        self.assertEqual(requested, [1, 2, 3])

    def test_bare_api_lists_are_fetched_until_the_short_page(self) -> None:
        pages = {
            1: [{"id": 1}, {"id": 2}],
            2: [{"id": 3}, {"id": 4}],
            3: [{"id": 5}],
        }

        def api_request(**kwargs: object) -> tuple[int, str, dict[str, str]]:
            page = int(kwargs["params"]["page"])  # type: ignore[index]
            return 200, json.dumps(pages[page]), {}

        with (
            patch.object(contests, "api_request_text", side_effect=api_request) as request,
            patch.object(contests, "request_text") as site,
        ):
            items = contests.load_competitions(
                COOKIES,
                BEARER,
                page=None,
                page_size=2,
                featured=None,
                all_pages=True,
            )

        self.assertEqual(items, [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}])
        self.assertEqual(request.call_count, 3)
        site.assert_not_called()

    def test_tasks_try_only_scoped_api_then_scoped_site(self) -> None:
        events: list[str] = []
        fallback = {
            "routes/competition/layout": {"data": {"taskList": [{"id": "task-1"}]}}
        }

        def api_request(**kwargs: object) -> tuple[int, str, dict[str, str]]:
            events.append(str(kwargs["path"]))
            return 404, "", {}

        def site_request(**kwargs: object) -> tuple[int, str, dict[str, str]]:
            events.append(str(kwargs["path"]))
            return 200, json.dumps(fallback), {}

        with (
            patch.object(contests, "api_request_text", side_effect=api_request),
            patch.object(contests, "request_text", side_effect=site_request) as site,
        ):
            tasks = contests.load_tasks(COOKIES, BEARER, "org", "contest")

        self.assertEqual(tasks, [{"id": "task-1"}])
        self.assertEqual(
            events,
            [
                "/organization/org/competition/contest/tasks",
                "/competitions/org/contest.data",
            ],
        )
        site.assert_called_once_with(
            path="/competitions/org/contest.data", cookies=COOKIES
        )

    def test_task_view_uses_api_first_and_site_fallback(self) -> None:
        with (
            patch.object(
                contests,
                "api_request_text",
                return_value=(200, '{"task":{"id":"7","title":"API"}}', {}),
            ) as api_request,
            patch.object(contests, "request_text") as site_request,
        ):
            result = contests.load_task_view(COOKIES, BEARER, "org", "contest", "7")
        self.assertEqual(result["task"]["title"], "API")
        api_request.assert_called_once_with(
            path="/organization/org/competition/contest/task/7", bearer=BEARER
        )
        site_request.assert_not_called()

        fallback = {
            "routes/task/layout": {"data": {"task": {"id": "7", "title": "Site"}}}
        }
        with (
            patch.object(contests, "api_request_text", return_value=(404, "", {})),
            patch.object(
                contests, "request_text", return_value=(200, json.dumps(fallback), {})
            ) as site_request,
        ):
            result = contests.load_task_view(COOKIES, BEARER, "org", "contest", "7")
        self.assertEqual(result["task"]["title"], "Site")
        site_request.assert_called_once_with(
            path="/competitions/org/contest/7/view.data", cookies=COOKIES
        )


class TaskFileTests(unittest.TestCase):
    def test_discovered_download_link_bypasses_api_and_preserves_query(self) -> None:
        link = "https://judge.nitro-ai.org/competitions/org/contest/7/train_data/download?token=x"
        with (
            patch.object(
                contests,
                "request",
                return_value=(200, b"csv", {"Content-Type": "text/csv"}),
            ) as site,
            patch.object(contests, "api_request_bytes") as api_request,
        ):
            result = contests.download_task_file(
                COOKIES,
                BEARER,
                "org",
                "contest",
                "7",
                "train-data",
                {"train_data": link},
            )
        self.assertEqual(result[1], b"csv")
        site.assert_called_once_with(
            path="/competitions/org/contest/7/train_data/download?token=x",
            cookies=COOKIES,
            timeout=180,
        )
        api_request.assert_not_called()

    def test_download_uses_api_binary_then_site_when_api_returns_html(self) -> None:
        with (
            patch.object(
                contests,
                "api_request_bytes",
                return_value=(
                    200,
                    b"<!doctype html><title>fallback</title>",
                    {"Content-Type": "text/html"},
                ),
            ) as api_request,
            patch.object(
                contests,
                "request",
                return_value=(200, b"PK\x03\x04data", {"Content-Type": "application/zip"}),
            ) as site_request,
        ):
            result = contests.download_task_file(
                COOKIES, BEARER, "org", "contest", "7", "train_data"
            )

        self.assertEqual(result[1], b"PK\x03\x04data")
        api_request.assert_called_once_with(
            path="/organization/org/competition/contest/task/7/file",
            bearer=BEARER,
            params={"file_category": "train_data"},
            timeout=180,
        )
        site_request.assert_called_once_with(
            path="/competitions/org/contest/7/train_data/download",
            cookies=COOKIES,
            timeout=180,
        )

    def test_download_returns_api_binary_without_site_fallback(self) -> None:
        with (
            patch.object(
                contests,
                "api_request_bytes",
                return_value=(200, b"a,b\n", {"Content-Type": "text/csv"}),
            ),
            patch.object(contests, "request") as site_request,
        ):
            status, body, _ = contests.download_task_file(
                COOKIES, BEARER, "org", "contest", "7", "train_data"
            )
        self.assertEqual((status, body), (200, b"a,b\n"))
        site_request.assert_not_called()

    def test_task_file_names_cover_server_name_zip_notebook_and_csv(self) -> None:
        self.assertEqual(
            contests.task_file_name(
                "train_data",
                {"Content-Disposition": "attachment; filename*=UTF-8''training%20set.csv"},
            ),
            "training set.csv",
        )
        self.assertEqual(
            contests.task_file_name(
                "train_data", {"Content-Disposition": 'attachment; filename="../../safe.csv"'}
            ),
            "safe.csv",
        )
        self.assertEqual(contests.task_file_name("train_data", {}, b"PK\x03\x04x"), "train_data.zip")
        notebook = json.dumps({"cells": [], "metadata": {}}).encode()
        self.assertEqual(
            contests.task_file_name("custom_archive", {}, notebook),
            "custom_archive.ipynb",
        )
        self.assertEqual(
            contests.task_file_name("sample_output", {"Content-Type": "text/csv"}),
            "sample_output.csv",
        )

    def test_download_data_writes_file_and_always_cleans_spinner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stop = object()
            thread = object()
            with (
                patch.object(contests, "load_task_file_links", return_value={}),
                patch.object(
                    contests,
                    "download_task_file",
                    return_value=(
                        200,
                        b"a,b\n1,2\n",
                        {"Content-Disposition": 'attachment; filename="training.csv"'},
                    ),
                ),
                patch.object(contests, "_start_spinner", return_value=(stop, thread)),
                patch.object(contests, "_stop_spinner") as stop_spinner,
            ):
                result = contests.download_task_data(
                    COOKIES,
                    BEARER,
                    "org",
                    "contest",
                    "7",
                    categories=["train_data"],
                    output_dir=directory,
                )

            target = Path(directory, "training.csv")
            self.assertEqual(target.read_bytes(), b"a,b\n1,2\n")
            self.assertEqual(
                result,
                [{"category": "train_data", "path": str(target), "bytes": 8}],
            )
            stop_spinner.assert_called_once_with(stop, thread)

    def test_download_data_extracts_zip_and_removes_archive(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("train/data.csv", "value\n1\n")

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(contests, "load_task_file_links", return_value={}),
                patch.object(
                    contests,
                    "download_task_file",
                    return_value=(
                        200,
                        archive.getvalue(),
                        {"Content-Disposition": 'attachment; filename="training.zip"'},
                    ),
                ),
                patch.object(contests, "_start_spinner", return_value=(object(), object())),
                patch.object(contests, "_stop_spinner"),
            ):
                result = contests.download_task_data(
                    COOKIES,
                    BEARER,
                    "org",
                    "contest",
                    "7",
                    categories=["train_data"],
                    output_dir=directory,
                )

            self.assertEqual(Path(directory, "train/data.csv").read_text(), "value\n1\n")
            self.assertFalse(Path(directory, "training.zip").exists())
            self.assertEqual(
                result,
                [{
                    "category": "train_data",
                    "path": str(Path(directory).resolve()),
                    "bytes": len(archive.getvalue()),
                    "extracted": True,
                }],
            )

    def test_possible_zip_bomb_is_kept_and_warned_about(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("large.csv", b"0" * 2048)

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(contests, "load_task_file_links", return_value={}),
                patch.object(
                    contests,
                    "download_task_file",
                    return_value=(
                        200,
                        archive.getvalue(),
                        {"Content-Disposition": 'attachment; filename="training.zip"'},
                    ),
                ),
                patch.object(contests, "ZIP_BOMB_MAX_UNCOMPRESSED_BYTES", 1024),
                patch.object(contests, "_start_spinner", return_value=(object(), object())),
                patch.object(contests, "_stop_spinner"),
            ):
                result = contests.download_task_data(
                    COOKIES,
                    BEARER,
                    "org",
                    "contest",
                    "7",
                    categories=["train_data"],
                    output_dir=directory,
                )

            self.assertTrue(Path(directory, "training.zip").exists())
            self.assertFalse(Path(directory, "large.csv").exists())
            self.assertIn("Possible zip bomb detected", result[0]["warning"])

    def test_output_constraints_and_overwrite_protection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "result.csv")
            target.write_bytes(b"old")
            with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                contests.write_task_file(
                    b"new", {}, "train_data", str(target), directory
                )
            contests.write_task_file(
                b"new", {}, "train_data", str(target), directory, force=True
            )
            self.assertEqual(target.read_bytes(), b"new")

            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                contests.download_task_data(
                    COOKIES,
                    BEARER,
                    "org",
                    "contest",
                    "7",
                    categories=["train_data", "test_data"],
                    output_path=str(target),
                )


class CommandExitTests(unittest.TestCase):
    def test_download_list_discovers_categories_without_writing(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                contests,
                "load_task_file_categories",
                return_value=["statement", "future_category"],
            ) as discover,
            patch.object(
                contests,
                "download_task_data",
                side_effect=AssertionError("download"),
            ),
            patch.object(
                contests,
                "write_task_file",
                side_effect=AssertionError("write"),
            ),
            contextlib.redirect_stdout(output),
        ):
            result = contests.cmd_download_data(
                COOKIES,
                BEARER,
                "org",
                "contest",
                "7",
                categories=None,
                output_dir=".",
                output_path=None,
                force=False,
                list_only=True,
            )

        self.assertEqual(result, 0)
        discover.assert_called_once_with(COOKIES, BEARER, "org", "contest", "7")
        self.assertEqual(
            output.getvalue().splitlines(),
            ["statement\tStatement", "future_category\tFuture category"],
        )

    def test_contest_and_task_commands_cache_successful_lists(self) -> None:
        competition = {"organizationSlug": "org", "competitionSlug": "contest", "title": "Contest"}
        task = {"id": "7", "title": "Task"}
        with (
            patch.object(contests, "load_competitions", return_value=[competition]),
            patch.object(contests, "update_cache") as cache,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(contests.cmd_contests(COOKIES, BEARER, 1, 20, True), 0)
        cache.assert_called_once_with("contests", "featured", [competition])

        output = io.StringIO()
        with (
            patch.object(contests, "load_tasks", return_value=[task]),
            patch.object(contests, "update_cache") as cache,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(contests.cmd_tasks(COOKIES, BEARER, "org", "contest"), 0)
        cache.assert_called_once_with("tasks", "org/contest", [task])
        self.assertEqual(output.getvalue().strip(), "[1] Task")

    def test_task_and_download_commands_return_one_on_domain_errors(self) -> None:
        output = io.StringIO()
        with (
            patch.object(contests, "load_task_view", side_effect=RuntimeError("missing")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(
                contests.cmd_task(COOKIES, BEARER, "org", "contest", "7"), 1
            )
        self.assertEqual(output.getvalue().strip(), "Error: missing")

        output = io.StringIO()
        with (
            patch.object(
                contests,
                "download_task_data",
                side_effect=RuntimeError("download failed"),
            ),
            contextlib.redirect_stdout(output),
        ):
            result = contests.cmd_download_data(
                COOKIES,
                BEARER,
                "org",
                "contest",
                "7",
                categories=["train_data"],
                output_dir=".",
                output_path=None,
                force=False,
            )
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue().strip(), "Error: download failed")

    def test_download_command_prints_written_results(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                contests,
                "download_task_data",
                return_value=[{"category": "train_data", "path": "data.csv", "bytes": 12}],
            ),
            contextlib.redirect_stdout(output),
        ):
            result = contests.cmd_download_data(
                COOKIES,
                BEARER,
                "org",
                "contest",
                "7",
                categories=["train_data"],
                output_dir=".",
                output_path=None,
                force=False,
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            output.getvalue().strip(),
            "Downloaded train_data -> data.csv (12 bytes)",
        )

    def test_download_command_prints_zip_bomb_warning(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                contests,
                "download_task_data",
                return_value=[{
                    "category": "train_data",
                    "path": "data.zip",
                    "bytes": 12,
                    "warning": "Possible zip bomb detected; archive kept",
                }],
            ),
            contextlib.redirect_stdout(output),
        ):
            result = contests.cmd_download_data(
                COOKIES,
                BEARER,
                "org",
                "contest",
                "7",
                categories=["train_data"],
                output_dir=".",
                output_path=None,
                force=False,
            )
        self.assertEqual(result, 0)
        self.assertIn("Warning: Possible zip bomb detected", output.getvalue())


if __name__ == "__main__":
    unittest.main()

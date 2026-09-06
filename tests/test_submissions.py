from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import io


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nitro_ai_judge_cli import config, submissions  # noqa: E402


class SubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = config.runtime()

    def tearDown(self) -> None:
        config._runtime = self.runtime

    def test_multipart_submission_uses_canonical_default_note_and_endpoint(self) -> None:
        config._runtime = config.RuntimeConfig(config.DEFAULT_API_BASE_URL, False)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "answer.csv")
            source = Path(directory, "solution.py")
            output.write_text("id,value\n1,2\n", encoding="utf-8")
            source.write_text("print('ok')\n", encoding="utf-8")
            response = json.dumps({"submissionID": "submission-id"})
            with patch.object(
                submissions, "api_request_text", return_value=(201, response, {})
            ) as request:
                result = submissions.create_submission(
                    ("cf", "session"),
                    "token",
                    "org",
                    "contest",
                    "7",
                    str(output),
                    str(source),
                    "",
                )
                wire_body = b"".join(request.call_args.kwargs["data"])

        self.assertEqual(result["submissionID"], "submission-id")
        kwargs = request.call_args.kwargs
        self.assertEqual(
            kwargs["path"], "/organization/org/competition/contest/task/7/submit"
        )
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["bearer"], "token")
        self.assertIn("multipart/form-data; boundary=----NAIJ", kwargs["headers"]["Content-Type"])
        self.assertIn(b'name="note"\r\n\r\nnaij', wire_body)
        self.assertIn(b'filename="answer.csv"', wire_body)
        self.assertIn(b'filename="solution.py"', wire_body)

    def test_proxy_submission_uses_json_payload_and_requires_source(self) -> None:
        config._runtime = config.RuntimeConfig("http://proxy.invalid", True)
        with self.assertRaisesRegex(RuntimeError, "--source is required"):
            submissions.create_submission(
                ("", ""), "token", "org", "contest", "7", "answer.csv", None, ""
            )

        response = json.dumps({"submissionId": "id"})
        with patch.object(
            submissions, "api_request_text", return_value=(200, response, {})
        ) as request:
            submissions.create_submission(
                ("", ""),
                "token",
                "org",
                "contest",
                "7",
                "answer.csv",
                "solution.py",
                "note",
            )
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["path"], "/task/7/submit")
        self.assertEqual(
            json.loads(kwargs["data"]),
            {
                "outputPath": "answer.csv",
                "sourceCodePath": "solution.py",
                "note": "note",
            },
        )

    def test_submission_accepts_async_202_response(self) -> None:
        response = json.dumps({"submissionID": "accepted-id"})
        config._runtime = config.RuntimeConfig(config.DEFAULT_API_BASE_URL, False)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "answer.csv")
            output.write_text("id,value\n1,2\n", encoding="utf-8")
            with patch.object(
                submissions,
                "api_request_text",
                return_value=(202, response, {}),
            ):
                direct = submissions.create_submission(
                    ("", ""),
                    "token",
                    "org",
                    "contest",
                    "7",
                    str(output),
                    None,
                    "",
                )

        config._runtime = config.RuntimeConfig("http://proxy.invalid", True)
        with patch.object(
            submissions,
            "api_request_text",
            return_value=(202, response, {}),
        ):
            proxy = submissions.create_submission(
                ("", ""),
                "token",
                "org",
                "contest",
                "7",
                "answer.csv",
                "solution.py",
                "",
            )

        self.assertEqual(direct["submissionID"], "accepted-id")
        self.assertEqual(proxy["submissionID"], "accepted-id")

    def test_submission_rejects_malformed_success_response(self) -> None:
        config._runtime = config.RuntimeConfig("http://proxy.invalid", True)
        with patch.object(
            submissions,
            "api_request_text",
            return_value=(200, "not json", {}),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Could not parse submission response"
            ):
                submissions.create_submission(
                    ("", ""),
                    "token",
                    "org",
                    "contest",
                    "7",
                    "answer.csv",
                    "solution.py",
                    "",
                )

    def test_submission_rejects_success_without_id(self) -> None:
        config._runtime = config.RuntimeConfig("http://proxy.invalid", True)
        with patch.object(
            submissions,
            "api_request_text",
            return_value=(202, json.dumps({"state": "queued"}), {}),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Submission response did not contain an ID"
            ):
                submissions.create_submission(
                    ("", ""),
                    "token",
                    "org",
                    "contest",
                    "7",
                    "answer.csv",
                    "solution.py",
                    "",
                )

    def test_submission_preserves_non_success_failure(self) -> None:
        config._runtime = config.RuntimeConfig("http://proxy.invalid", True)
        with patch.object(
            submissions,
            "api_request_text",
            return_value=(422, '{"detail":"invalid output"}', {}),
        ):
            with self.assertRaisesRegex(
                RuntimeError, r"HTTP 422:.*invalid output"
            ):
                submissions.create_submission(
                    ("", ""),
                    "token",
                    "org",
                    "contest",
                    "7",
                    "answer.csv",
                    "solution.py",
                    "",
                )

    def test_submission_list_preserves_endpoint_and_parameters(self) -> None:
        body = json.dumps({"items": [{"id": "one"}], "lastPage": 3})
        with patch.object(
            submissions, "api_request_text", return_value=(200, body, {})
        ) as request:
            items, pages = submissions.load_submissions(
                ("cf", "session"),
                "token",
                "org",
                "contest",
                "7",
                author="alice",
                page=2,
                page_size=25,
                mode="complete",
            )
        self.assertEqual(items, [{"id": "one"}])
        self.assertEqual(pages, 3)
        self.assertEqual(
            request.call_args.kwargs,
            {
                "path": "/organization/org/competition/contest/task/7/submissions",
                "bearer": "token",
                "params": {
                    "author": "alice",
                    "page": 2,
                    "page_size": 25,
                    "scoring_mode": "complete",
                },
            },
        )

    def test_submission_list_uses_nested_pagination_metadata(self) -> None:
        pages: list[int] = []

        def request(**kwargs: object) -> tuple[int, str, dict[str, str]]:
            page = kwargs["params"]["page"]  # type: ignore[index]
            pages.append(page)
            return 200, json.dumps(
                {
                    "partialSubmissions": {
                        "data": [{"id": f"p{page}"}],
                        "lastPage": 2,
                    }
                }
            ), {}

        with patch.object(submissions, "api_request_text", side_effect=request):
            items, last_page = submissions.load_submissions(
                ("", ""),
                "token",
                "org",
                "contest",
                "7",
                author=None,
                page=None,
                page_size=10,
                mode="partial",
            )

        self.assertEqual(items, [{"id": "p1"}, {"id": "p2"}])
        self.assertEqual(last_page, 2)
        self.assertEqual(pages, [1, 2])

    def test_short_subtask_arrays_render_missing_values(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            submissions.print_submission_details(
                {
                    "subtasks": [{"id": 1}, {"id": 2}],
                    "partialSubtaskScores": [42],
                    "partialSubtaskMetricValues": [0.5],
                    "completeTaskScore": 42,
                    "completeSubtaskScores": [42],
                    "completeSubtaskMetricValues": [0.5],
                }
            )

        self.assertIn("#2 partial None/? | complete None/?", output.getvalue())
    def test_submission_pagination_is_capped_and_stops_on_repeated_pages(self) -> None:
        requested: list[int] = []

        def unique_request(**kwargs: object) -> tuple[int, str, dict[str, str]]:
            page = int(kwargs["params"]["page"])  # type: ignore[index]
            requested.append(page)
            return 200, json.dumps({"items": [{"id": page}], "lastPage": 10**9}), {}

        with (
            patch.object(submissions, "MAX_PAGINATION_PAGES", 4),
            patch.object(submissions, "api_request_text", side_effect=unique_request),
        ):
            items, last_page = submissions.load_submissions(
                ("", ""), "token", "org", "contest", "7",
                author=None, page=None, page_size=10, mode="partial",
            )
        self.assertEqual(requested, [1, 2, 3, 4])
        self.assertEqual(len(items), 4)
        self.assertEqual(last_page, 4)

        requested.clear()

        def repeated_request(**kwargs: object) -> tuple[int, str, dict[str, str]]:
            page = int(kwargs["params"]["page"])  # type: ignore[index]
            requested.append(page)
            return 200, json.dumps(
                {"items": [{"id": min(page, 2)}], "lastPage": 10**9}
            ), {}

        with patch.object(
            submissions, "api_request_text", side_effect=repeated_request
        ):
            submissions.load_submissions(
                ("", ""), "token", "org", "contest", "7",
                author=None, page=None, page_size=10, mode="partial",
            )
        self.assertEqual(requested, [1, 2, 3])

    def test_polling_returns_first_non_pending_feedback(self) -> None:
        pending = {"id": "one", "state": "pending"}
        complete = {"id": "one", "state": "complete"}
        with (
            patch.object(submissions, "load_submission", side_effect=[pending, complete]) as load,
            patch.object(submissions.time, "sleep") as sleep,
        ):
            result = submissions.poll_submission_feedback(
                ("", ""), "token", "one", interval=1, timeout=30
            )
        self.assertEqual(result, complete)
        self.assertEqual(load.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_final_selection_uses_expected_action(self) -> None:
        with patch.object(
            submissions, "api_request_text", return_value=(200, "", {})
        ) as request:
            submissions.set_submission_final(("", ""), "token", "id", True)
            submissions.set_submission_final(("", ""), "token", "id", False)
        self.assertEqual(
            [call.kwargs["path"] for call in request.call_args_list],
            ["/submission/id/setFinal", "/submission/id/unsetFinal"],
        )
        self.assertTrue(all(call.kwargs["method"] == "POST" for call in request.call_args_list))

    def test_submit_command_reports_io_errors_as_exit_one(self) -> None:
        with (
            patch.object(submissions, "create_submission", side_effect=OSError("missing")),
            patch("builtins.print") as output,
        ):
            result = submissions.cmd_submit(
                ("", ""), "token", "org", "contest", "7", "missing.csv", None, "", False
            )
        self.assertEqual(result, 1)
        self.assertIn("missing", output.call_args.args[0])

    def test_submit_wait_accepts_canonical_id(self) -> None:
        feedback = {"id": "canonical-123", "state": "complete"}
        with (
            patch.object(
                submissions, "create_submission", return_value={"id": "canonical-123"}
            ),
            patch.object(
                submissions, "poll_submission_feedback", return_value=feedback
            ) as poll,
            patch.object(submissions, "print_submission_details"),
        ):
            result = submissions.cmd_submit(
                ("", ""),
                "token",
                "org",
                "contest",
                "7",
                "answer.csv",
                None,
                "",
                True,
            )

        self.assertEqual(result, 0)
        poll.assert_called_once()
        self.assertEqual(poll.call_args.args[2], "canonical-123")

    def test_existing_submission_wait_timeout_and_interrupt_have_distinct_results(self) -> None:
        with patch.object(
            submissions,
            "poll_submission_feedback",
            side_effect=submissions.SubmissionWaitTimeout("timed out"),
        ):
            self.assertEqual(
                submissions.cmd_wait_submission(("", ""), "token", "id"), 2
            )

        with patch.object(
            submissions, "poll_submission_feedback", side_effect=KeyboardInterrupt
        ):
            self.assertEqual(
                submissions.cmd_wait_submission(("", ""), "token", "id"), 130
            )


if __name__ == "__main__":
    unittest.main()

"""Regression tests for task-file category and filename safety."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nitro_ai_judge_cli import contests  # noqa: E402


COOKIES = ("clearance", "session")
BEARER = "access-token"


class TaskFileCategoryAdditionTests(unittest.TestCase):
    def test_safe_unknown_category_is_normalized_and_downloaded(self) -> None:
        self.assertEqual(
            contests.normalize_task_file_category("  Custom-Data  "), "custom_data"
        )

        with patch.object(
            contests, "api_request_bytes", return_value=(200, b"data", {})
        ) as request:
            status, body, _ = contests.download_task_file(
                COOKIES, BEARER, "org", "contest", "7", "Custom-Data"
            )

        self.assertEqual((status, body), (200, b"data"))
        request.assert_called_once_with(
            path="/organization/org/competition/contest/task/7/file",
            bearer=BEARER,
            params={"file_category": "custom_data"},
            timeout=180,
        )

    def test_malicious_categories_are_rejected_before_requests(self) -> None:
        malicious = ("../secrets", "data/file", "data\\file", "data:file", "..")
        with patch.object(contests, "api_request_bytes") as request:
            for category in malicious:
                with self.subTest(category=category):
                    with self.assertRaises(ValueError):
                        contests.download_task_file(
                            COOKIES, BEARER, "org", "contest", "7", category
                        )
        request.assert_not_called()

    def test_link_parser_only_accepts_downloads_for_the_same_task(self) -> None:
        parser = contests.TaskFileLinkParser("org", "contest", "7")
        parser.feed(
            '<a href="/competitions/org/contest/7/custom-data/download">same</a>'
            '<a href="/competitions/org/contest/8/other-data/download">other task</a>'
            '<a href="/competitions/other/contest/7/third-data/download">other org</a>'
        )

        self.assertEqual(
            parser.links,
            {"custom_data": "/competitions/org/contest/7/custom-data/download"},
        )

    def test_content_disposition_filename_strips_both_separators_and_rejects_colons(self) -> None:
        self.assertEqual(
            contests.filename_from_content_disposition(
                'attachment; filename="../nested\\report.txt"'
            ),
            "report.txt",
        )
        self.assertIsNone(
            contests.filename_from_content_disposition(
                'attachment; filename="report:secret.txt"'
            )
        )

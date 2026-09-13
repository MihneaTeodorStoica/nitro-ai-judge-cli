from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nitro_ai_judge_cli import submissions  # noqa: E402


class SubmissionCacheTests(unittest.TestCase):
    def test_cached_items_include_mode_and_query_key_identifies_filters(self) -> None:
        updates: list[tuple[str, str, list[dict[str, object]]]] = []

        with (
            patch.object(
                submissions, "load_submissions", return_value=([{"id": "one"}], 1)
            ),
            patch.object(submissions, "load_state", return_value={"username": "me"}),
            patch.object(submissions, "update_cache", side_effect=lambda *args: updates.append(args)),
            patch.object(submissions, "print_submissions"),
        ):
            result = submissions.cmd_submissions(
                ("cookie", "session"), "token", "org", "comp", "task",
                author="me", page=2, page_size=25, mode="partial",
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            updates,
            [
                (
                    "submissions",
                    "org/comp/task?author=me&page=2&page_size=25&mode=partial",
                    [{"id": "one", "_mode": "partial"}],
                )
            ],
        )

    def test_narrow_query_does_not_replace_canonical_task_cache(self) -> None:
        updates: list[tuple[str, str, list[dict[str, object]]]] = []

        with (
            patch.object(
                submissions, "load_submissions", return_value=([{"id": "one"}], 1)
            ),
            patch.object(submissions, "load_state", return_value={"username": "me"}),
            patch.object(submissions, "update_cache", side_effect=lambda *args: updates.append(args)),
            patch.object(submissions, "print_submissions"),
        ):
            submissions.cmd_submissions(
                ("cookie", "session"), "token", "org", "comp", "task",
                author="me", page=1, page_size=50, mode="both",
            )

        self.assertEqual(len(updates), 1)
        self.assertNotEqual(updates[0][1], "org/comp/task")

    def test_full_both_mode_for_current_user_updates_canonical_cache(self) -> None:
        updates: list[tuple[str, str, list[dict[str, object]]]] = []

        with (
            patch.object(
                submissions,
                "load_submissions",
                side_effect=[([{"id": "partial"}], 2), ([{"id": "complete"}], 2)],
            ) as load_submissions,
            patch.object(submissions, "load_state", return_value={"username": "me"}),
            patch.object(submissions, "update_cache", side_effect=lambda *args: updates.append(args)),
            patch.object(submissions, "print_submissions"),
        ):
            result = submissions.cmd_submissions(
                ("cookie", "session"), "token", "org", "comp", "task",
                author="me", page=None, page_size=50, mode="both",
            )

        self.assertEqual(result, 0)
        self.assertEqual([call.kwargs["mode"] for call in load_submissions.call_args_list], ["partial", "complete"])
        expected = [
            {"id": "partial", "_mode": "partial"},
            {"id": "complete", "_mode": "complete"},
        ]
        self.assertEqual(
            updates,
            [
                (
                    "submissions",
                    "org/comp/task?author=me&page=all&page_size=50&mode=both",
                    expected,
                ),
                ("submissions", "org/comp/task", expected),
            ],
        )


if __name__ == "__main__":
    unittest.main()

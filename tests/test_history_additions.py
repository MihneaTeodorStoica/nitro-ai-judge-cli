from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from nitro_ai_judge_cli.manager.store import ManagerStore

try:
    from nitro_ai_judge_cli.manager.app import operations
    from nitro_ai_judge_cli.play_protocol import WireError
except ImportError:  # pragma: no cover - host-only installs omit aiohttp
    operations = WireError = None


class ManagerStoreOperationsTests(unittest.TestCase):
    def test_operations_orders_by_update_and_clamps_lower_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ManagerStore(f"{directory}/manager.db")
            try:
                with patch(
                    "nitro_ai_judge_cli.manager.store.time.time",
                    side_effect=(10, 20, 30, 40),
                ):
                    store.create_operation("alpha", "org/contest", "pull", {})
                    store.create_operation("bravo", "org/contest", "play", {})
                    store.create_operation("charlie", "org/contest", "start", {})
                    store.event("alpha", "pulling", "Updated last")

                self.assertEqual(
                    [item["id"] for item in store.operations(limit=2)],
                    ["alpha", "charlie"],
                )
                self.assertEqual(
                    [item["id"] for item in store.operations(limit=0)], ["alpha"]
                )
            finally:
                store.close()

    def test_operations_clamps_upper_limit_to_two_hundred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ManagerStore(f"{directory}/manager.db")
            try:
                for number in range(201):
                    store.create_operation(
                        f"operation-{number:03d}", "org/contest", "pull", {}
                    )

                values = store.operations(limit=999)
                self.assertEqual(len(values), 200)
                self.assertNotIn("operation-000", [item["id"] for item in values])
            finally:
                store.close()


@unittest.skipIf(operations is None, "aiohttp manager extra is not installed")
class OperationsHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_limit_returns_summaries_without_options_or_results(self) -> None:
        store = MagicMock()
        store.operations.return_value = [
            {
                "id": "operation-1",
                "competition": "org/contest",
                "action": "pull",
                "options": {"token": "saved-secret"},
                "result": {"access_token": "result-secret"},
                "error": {"logs": ["private log"]},
                "status": "complete",
                "stage": "complete",
                "message": "Operation complete",
                "created_at": 10,
                "updated_at": 20,
            }
        ]
        request = MagicMock()
        request.query = {"limit": "2"}
        request.app = {"store": store}

        response = await operations(request)

        store.operations.assert_called_once_with(limit=2)
        body = json.loads(response.text)
        self.assertEqual(
            body,
            {
                "operations": [
                    {
                        "id": "operation-1",
                        "competition": "org/contest",
                        "action": "pull",
                        "status": "complete",
                        "stage": "complete",
                        "message": "Operation complete",
                        "created_at": 10,
                        "updated_at": 20,
                    }
                ]
            },
        )

    async def test_malformed_or_out_of_range_limits_are_rejected(self) -> None:
        for limit in ("not-a-number", "", "0", "-1", "201"):
            with self.subTest(limit=limit):
                request = MagicMock()
                request.query = {"limit": limit}
                with self.assertRaises(WireError) as raised:
                    await operations(request)
                self.assertEqual(raised.exception.status, 400)

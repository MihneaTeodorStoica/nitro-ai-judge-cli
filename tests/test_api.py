from __future__ import annotations

import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nitro_ai_judge_cli import api, config  # noqa: E402


class _Response:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"ok",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class TransportTests(unittest.TestCase):
    def test_request_preserves_url_method_auth_headers_and_user_agent(self) -> None:
        response = _Response(201, b'{}', {"X-Reply": "yes"})
        with patch.object(api, "_open_once", return_value=response) as open_url:
            result = api.request(
                "/entries",
                cookies=("clearance", "session"),
                method="POST",
                params={"page": 2, "featured": "true", "empty": None},
                bearer="access-token",
                headers={"X-Test": "value"},
                data=b"payload",
                timeout=17,
                base_url="https://api.invalid/root/",
            )

        self.assertEqual(result, (201, b'{}', {"X-Reply": "yes"}))
        request = open_url.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.invalid/root/entries?page=2&featured=true",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"payload")
        self.assertEqual(request.get_header("Cookie"), "cf_clearance=clearance; Cookie=session")
        self.assertEqual(request.get_header("Authorization"), "Bearer access-token")
        self.assertEqual(request.get_header("X-test"), "value")
        self.assertEqual(request.get_header("User-agent"), config.USER_AGENT)
        self.assertTrue(config.USER_AGENT.startswith("NAIJ/"))
        self.assertEqual(open_url.call_args.args[1], 17)

    def test_request_returns_http_error_body_and_headers(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.invalid/denied",
            403,
            "Forbidden",
            {"X-Reason": "denied"},
            io.BytesIO(b"no access"),
        )
        with patch.object(api, "_open_once", side_effect=error):
            result = api.request("/denied", base_url="https://api.invalid")
        self.assertEqual(result, (403, b"no access", {"X-Reason": "denied"}))

    def test_same_origin_canonical_redirect_is_followed(self) -> None:
        responses = [
            _Response(302, b"", {"Location": "/entries/"}),
            _Response(200, b"done"),
        ]
        with patch.object(api, "_open_once", side_effect=responses) as open_url:
            result = api.request(
                "/entries",
                bearer="secret",
                base_url="https://api.invalid",
            )
        self.assertEqual(result[:2], (200, b"done"))
        self.assertEqual(
            [call.args[0].full_url for call in open_url.call_args_list],
            ["https://api.invalid/entries", "https://api.invalid/entries/"],
        )
        self.assertEqual(
            open_url.call_args_list[1].args[0].get_header("Authorization"),
            "Bearer secret",
        )

    def test_login_redirect_becomes_actionable_authentication_error(self) -> None:
        with patch.object(
            api,
            "_open_once",
            return_value=_Response(302, b"", {"Location": "/login"}),
        ):
            with self.assertRaisesRegex(
                api.AuthenticationRedirect, "sign in again"
            ):
                api.request(
                    "/competitions",
                    bearer="expired",
                    base_url="https://judge.invalid/api",
                )

    def test_authenticated_401_becomes_refreshable_authentication_error(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.invalid/private",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"expired"),
        )
        with patch.object(api, "_open_once", side_effect=error):
            with self.assertRaisesRegex(
                api.AuthenticationRequired, "sign in again"
            ):
                api.request(
                    "/private",
                    bearer="expired",
                    base_url="https://api.invalid",
                )

    def test_credential_bearing_cross_origin_redirect_is_rejected(self) -> None:
        with patch.object(
            api,
            "_open_once",
            return_value=_Response(
                302,
                b"",
                {"Location": "https://attacker.invalid/collect"},
            ),
        ) as open_url:
            with self.assertRaisesRegex(
                api.RedirectError, "Refused to send credentials"
            ):
                api.request(
                    "/private",
                    cookies=("clearance", "session"),
                    bearer="secret",
                    base_url="https://judge.invalid",
                )
        self.assertEqual(open_url.call_count, 1)

    def test_api_client_keeps_api_and_site_roots_separate(self) -> None:
        client = api.APIClient(
            "https://proxy.invalid/api", ("clearance", "session"), "bearer"
        )
        with patch.object(
            api, "request", return_value=(200, b"text", {"Content-Type": "text/plain"})
        ) as transport:
            self.assertEqual(client.api_text("/items")[1], "text")
            api_call = transport.call_args
            self.assertEqual(api_call.args, ("/items",))
            self.assertEqual(api_call.kwargs["base_url"], "https://proxy.invalid/api")
            self.assertEqual(api_call.kwargs["cookies"], ("clearance", "session"))
            self.assertEqual(api_call.kwargs["bearer"], "bearer")

            client.site_text("/competitions.data")
            site_call = transport.call_args
            self.assertEqual(site_call.args, ("/competitions.data",))
            self.assertEqual(site_call.kwargs["base_url"], config.BASE_URL)


class PayloadTests(unittest.TestCase):
    def test_singlefetch_and_nested_payload_parsing(self) -> None:
        encoded = json.dumps([{"_1": 2}, "field", "value"])
        self.assertEqual(
            api.parse_singlefetch(encoded),
            [{"field": "value"}, "field", "value"],
        )
        self.assertIsNone(api.parse_singlefetch("not-json"))
        self.assertEqual(
            api.list_payload(
                {"data": {"items": [{"id": 1}]}}, "items", "data"
            ),
            [{"id": 1}],
        )
        self.assertEqual(
            api.int_payload({"data": {"totalPages": "4"}}, "totalPages"), 4
        )
        self.assertIsNone(api.body_json("{"))

    def test_multipart_contains_fields_files_and_matching_boundary(self) -> None:
        body, boundary = api.build_multipart(
            {"note": "NAIJ"},
            {"output": ("submission.csv", b"a,b\n1,2\n", "text/csv")},
        )
        self.assertTrue(boundary.startswith("----NAIJ"))
        self.assertIn(f"--{boundary}\r\n".encode(), body)
        self.assertIn(b'name="note"\r\n\r\nNAIJ', body)
        self.assertIn(b'name="output"; filename="submission.csv"', body)
        self.assertIn(b"Content-Type: text/csv", body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))


class AuthenticationTests(unittest.TestCase):
    def test_malformed_credential_fields_do_not_raise(self) -> None:
        self.assertEqual(
            api.get_auth({"access_token": "abc", "cookies": None}),
            (None, None, "abc"),
        )
        self.assertIsNone(api.get_auth({"access_token": 123, "cookies": [None]}))

        encoded = urllib.parse.quote(base64.b64encode(b"[]").decode())
        self.assertIsNone(api.decode_session(encoded))

    def test_login_request_and_success_parsing(self) -> None:
        payload = {"accessToken": "access", "refreshToken": "refresh"}
        with patch.object(
            api,
            "api_request_text",
            return_value=(200, json.dumps(payload), {"x-set-username": "canonical"}),
        ) as request:
            result = api.do_login("input-user", "secret")

        self.assertTrue(result["success"])
        self.assertEqual(result["tokens"], payload)
        self.assertEqual(result["username"], "canonical")
        call = request.call_args.kwargs
        self.assertEqual(call["path"], "/auth/login")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["timeout"], 20)
        self.assertEqual(
            call["headers"], {"Content-Type": "application/x-www-form-urlencoded"}
        )
        form = urllib.parse.parse_qs(call["data"].decode())
        self.assertEqual(form["username"], ["input-user"])
        self.assertEqual(form["password"], [api.hash_password("secret")])

    def test_login_failure_uses_server_message(self) -> None:
        with patch.object(
            api,
            "api_request_text",
            return_value=(401, '{"message":"bad credentials"}', {}),
        ):
            result = api.do_login("user", "wrong")
        self.assertFalse(result["success"])
        self.assertEqual(result["http_code"], 401)
        self.assertEqual(result["error"], "bad credentials")

    def test_refresh_posts_refresh_token_and_persists_normalized_result(self) -> None:
        refreshed_payload = {
            "accessToken": "new-access",
            "refreshToken": "new-refresh",
            "role": "contestant",
        }
        saved_state = {"access_token": "new-access", "username": "alice"}
        with (
            patch.object(
                api,
                "api_request_text",
                return_value=(200, json.dumps(refreshed_payload), {}),
            ) as request,
            patch.object(api, "save_token_state") as save,
            patch.object(api, "load_state", return_value=saved_state),
        ):
            result = api.refresh_saved_tokens(
                {"refresh_token": "old-refresh", "username": "alice"}
            )

        self.assertIs(result, saved_state)
        call = request.call_args.kwargs
        self.assertEqual(call["path"], "/auth/refreshToken")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            urllib.parse.parse_qs(call["data"].decode()),
            {"refreshToken": ["old-refresh"]},
        )
        normalized, username = save.call_args.args
        self.assertEqual(normalized["access_token"], "new-access")
        self.assertEqual(normalized["refresh_token"], "new-refresh")
        self.assertEqual(username, "alice")

    def test_refresh_reads_legacy_session_cookie(self) -> None:
        session = urllib.parse.quote(
            base64.b64encode(
                json.dumps({"refreshToken": "cookie-refresh"}).encode()
            ).decode()
        )
        state = {"username": "alice", "cookies": [{"name": "Cookie", "value": session}]}
        with patch.object(api, "api_request_text", return_value=(500, "", {})) as request:
            self.assertIsNone(api.refresh_saved_tokens(state))
        form = urllib.parse.parse_qs(request.call_args.kwargs["data"].decode())
        self.assertEqual(form, {"refreshToken": ["cookie-refresh"]})

    def test_cmd_login_preserves_success_and_failure_exit_codes(self) -> None:
        success = {
            "success": True,
            "tokens": {"accessToken": "access", "refreshToken": "refresh"},
            "username": "alice",
        }
        output = io.StringIO()
        with (
            patch.object(api, "do_login", return_value=success),
            patch.object(api, "save_token_state") as save,
            patch.object(api, "load_state", return_value={"username": "alice", "role": "user"}),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(api.cmd_login("alice", "secret"), 0)
        save.assert_called_once()
        self.assertIn("Login OK | user=alice | role=user", output.getvalue())

        output = io.StringIO()
        with (
            patch.object(
                api,
                "do_login",
                return_value={"success": False, "tokens": None, "error": "denied"},
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(api.cmd_login("alice", "wrong"), 1)
        self.assertEqual(output.getvalue().strip(), "Login failed: denied")

    def test_cmd_login_redacts_password_from_failure_output(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                api,
                "do_login",
                return_value={
                    "success": False,
                    "tokens": None,
                    "error": "rejected visible-secret",
                },
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(api.cmd_login("alice", "visible-secret"), 1)

        self.assertNotIn("visible-secret", output.getvalue())
        self.assertIn("[redacted]", output.getvalue())
    def test_cmd_login_reports_redirect_errors_without_traceback(self) -> None:
        for error in (
            api.AuthenticationRedirect("Authentication required"),
            api.RedirectError("Refused cross-origin redirect"),
        ):
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with (
                    patch.object(api, "do_login", side_effect=error),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(api.cmd_login("alice", "secret"), 1)
                self.assertEqual(output.getvalue().strip(), f"Login failed: {error}")


if __name__ == "__main__":
    unittest.main()

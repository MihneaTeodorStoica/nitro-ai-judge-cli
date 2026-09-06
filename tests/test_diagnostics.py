from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nitro_ai_judge_cli import diagnostics, state  # noqa: E402


class DiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.home = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True))
        stack.enter_context(patch.object(state, "_cli_state_dir", None))
        stack.enter_context(patch.object(state, "_cached_paths", None))
        stack.enter_context(patch.object(state, "_cached_key", None))
        self.which = stack.enter_context(
            patch.object(diagnostics.shutil, "which", return_value=None)
        )
        self.run = stack.enter_context(patch.object(diagnostics.subprocess, "run"))
        stack.enter_context(
            patch.object(
                diagnostics,
                "runtime",
                return_value=SimpleNamespace(
                    api_base_url="https://example.test/api", submission_proxy=False
                ),
            )
        )

    @contextlib.contextmanager
    def forbid_state_mutations(self):
        with contextlib.ExitStack() as stack:
            for name in ("chmod", "mkdir", "makedirs", "rename", "replace"):
                stack.enter_context(
                    patch.object(os, name, side_effect=AssertionError(f"unexpected {name}"))
                )
            stack.enter_context(
                patch.object(
                    diagnostics.shutil, "move", side_effect=AssertionError("unexpected move")
                )
            )
            yield

    def test_legacy_state_is_inspected_without_migration_or_chmod(self) -> None:
        legacy = self.home / ".nitro-cli"
        legacy.mkdir(mode=0o755)
        paths = state.inspect_state_paths()
        credentials = Path(paths.credentials)
        credentials.write_text('{"token":"legacy-secret"}', encoding="utf-8")
        credentials.chmod(0o644)
        before = (legacy.stat().st_mode, credentials.stat().st_mode)

        with self.forbid_state_mutations():
            inspected = state.inspect_state_paths()
            report = diagnostics.collect_diagnostics()

        self.assertEqual(inspected.root, str(legacy))
        self.assertEqual(report["state_dir"], str(legacy))
        self.assertEqual(report["credentials_file"], "present")
        self.assertEqual(report["state_permissions"], oct(stat.S_IMODE(before[0])))
        self.assertFalse((self.home / ".naij").exists())
        self.assertEqual(before, (legacy.stat().st_mode, credentials.stat().st_mode))
        self.assertEqual(credentials.read_text(), '{"token":"legacy-secret"}')
        self.run.assert_not_called()

    def test_missing_state_is_not_created(self) -> None:
        for override in (None, str(self.home / "custom" / "state")):
            with self.subTest(override=override), patch.dict(
                os.environ, {} if override is None else {"NAIJ_STATE_DIR": override}
            ):
                with self.forbid_state_mutations():
                    paths = state.inspect_state_paths()
                    report = diagnostics.collect_diagnostics()
                self.assertEqual(paths.root, override or str(self.home / ".naij"))
                self.assertEqual(report["state_dir"], paths.root)
                self.assertEqual(report["credentials_file"], "missing")
                self.assertFalse(Path(paths.root).exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_inspection_prefers_canonical_without_modifying_legacy(self) -> None:
        for name in (".naij", ".nitro-cli"):
            (self.home / name).mkdir()
        with self.forbid_state_mutations():
            self.assertEqual(state.inspect_state_paths().root, str(self.home / ".naij"))
        self.assertTrue((self.home / ".nitro-cli").is_dir())

    def test_invalid_credentials_are_reported_without_exposing_contents(self) -> None:
        root = self.home / ".naij"
        root.mkdir()
        credentials = Path(state.inspect_state_paths().credentials)
        for content in ('{"token":"private-broken-json"', '["private-list-token"]', "null"):
            with self.subTest(content=content):
                credentials.write_text(content, encoding="utf-8")
                with self.forbid_state_mutations():
                    report = diagnostics.collect_diagnostics()
                self.assertEqual(report["credentials_file"], "invalid")
                self.assertNotIn("private-", json.dumps(report))
                self.assertEqual(credentials.read_text(), content)

    def test_secrets_are_absent_from_report_and_output(self) -> None:
        (self.home / ".naij").mkdir()
        secrets = ["private-token", "private-cookie", "private-user", "private-password",
                   "private-query", "private-fragment"]
        Path(state.inspect_state_paths().credentials).write_text(
            json.dumps({"token": secrets[0], "cookies": secrets[1]}), encoding="utf-8"
        )
        runtime = SimpleNamespace(
            api_base_url="https://private-user:private-password@example.test/api"
                         "?token=private-query#private-fragment",
            submission_proxy=False,
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(diagnostics, "runtime", return_value=runtime),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            report = diagnostics.collect_diagnostics()
        self.assertEqual(report["credentials_file"], "present")
        self.assertEqual(report["api_url"], "https://example.test/api")
        output = json.dumps(report) + stdout.getvalue() + stderr.getvalue()
        for secret in secrets:
            self.assertNotIn(secret, output)

    def test_container_probes_are_bounded_and_discard_output(self) -> None:
        self.which.side_effect = lambda name: f"/mock/bin/{name}"
        self.run.side_effect = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]
        report = diagnostics.collect_diagnostics()
        self.assertEqual(report["container_tools"], {
            "podman": "compose available", "docker": "compose unavailable"
        })
        self.assertEqual(self.run.call_args_list, [
            call([f"/mock/bin/{name}", "compose", "version"],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            for name in ("podman", "docker")
        ])

    def test_probe_timeout_and_oserror_are_handled(self) -> None:
        self.which.side_effect = lambda name: f"/mock/bin/{name}"
        self.run.side_effect = [subprocess.TimeoutExpired("compose", 5), OSError("unavailable")]
        report = diagnostics.collect_diagnostics()
        self.assertEqual(report["container_tools"], {"podman": "unavailable", "docker": "unavailable"})
        self.assertEqual(self.run.call_count, 2)

    def test_missing_tools_are_not_executed(self) -> None:
        report = diagnostics.collect_diagnostics()
        self.assertEqual(report["container_tools"], {"podman": "missing", "docker": "missing"})
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

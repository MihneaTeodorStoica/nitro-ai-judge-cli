from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nitro_ai_judge_cli import completion, config, state  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_canonical_api_url_beats_legacy_and_proxy(self) -> None:
        env = {
            "NAIJ_API_BASE_URL": " https://canonical.invalid/api/ ",
            "NITRO_API_BASE_URL": "https://legacy.invalid/api",
            "PROXY_URL": "https://proxy.invalid",
        }
        with patch.dict(os.environ, env, clear=True):
            runtime = config.RuntimeConfig.resolve()
        self.assertEqual(runtime.api_base_url, "https://canonical.invalid/api")
        self.assertFalse(runtime.submission_proxy)

    def test_cli_api_url_beats_all_environment_urls(self) -> None:
        env = {
            "NAIJ_API_BASE_URL": "https://canonical.invalid",
            "NITRO_API_BASE_URL": "https://legacy.invalid",
            "PROXY_URL": "https://proxy.invalid",
        }
        with patch.dict(os.environ, env, clear=True):
            runtime = config.RuntimeConfig.resolve(" https://cli.invalid/api/ ")
        self.assertEqual(runtime.api_base_url, "https://cli.invalid/api")
        self.assertFalse(runtime.submission_proxy)

    def test_legacy_and_generic_fallbacks_remain_supported(self) -> None:
        with patch.dict(
            os.environ,
            {"NITRO_API_BASE_URL": " https://legacy.invalid/api/ "},
            clear=True,
        ):
            legacy = config.RuntimeConfig.resolve()
        self.assertEqual(legacy.api_base_url, "https://legacy.invalid/api")
        self.assertFalse(legacy.submission_proxy)

        with patch.dict(
            os.environ, {"PROXY_URL": " https://proxy.invalid/ "}, clear=True
        ):
            proxy = config.RuntimeConfig.resolve()
        self.assertEqual(proxy.api_base_url, "https://proxy.invalid")
        self.assertTrue(proxy.submission_proxy)

    def test_canonical_false_proxy_value_blocks_lower_precedence_values(self) -> None:
        env = {
            "NAIJ_SUBMISSION_PROXY": "false",
            "NITRO_SUBMISSION_PROXY": "yes",
            "PROXY_URL": "https://proxy.invalid",
        }
        with patch.dict(os.environ, env, clear=True):
            runtime = config.RuntimeConfig.resolve()
        self.assertFalse(runtime.submission_proxy)

    def test_cli_proxy_flag_beats_explicit_false_environment_value(self) -> None:
        with patch.dict(
            os.environ, {"NAIJ_SUBMISSION_PROXY": "off"}, clear=True
        ):
            runtime = config.RuntimeConfig.resolve(submission_proxy=True)
        self.assertTrue(runtime.submission_proxy)


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)
        self.environment.start()
        state.reset_state_paths()
        state._warned.clear()

    def tearDown(self) -> None:
        state.configure_state_dir(None)
        state.reset_state_paths()
        state._warned.clear()
        self.environment.stop()
        self.temporary.cleanup()

    def set_state_root(self, name: str = "state") -> Path:
        root = self.home / name
        os.environ["NAIJ_STATE_DIR"] = str(root)
        state.reset_state_paths()
        return root

    def test_old_default_directory_is_renamed_without_copying(self) -> None:
        old = self.home / ".nitro-cli"
        old.mkdir()
        marker = old / "marker"
        marker.write_text("kept", encoding="utf-8")

        paths = state.resolve_state_paths()

        new = self.home / ".naij"
        self.assertEqual(Path(paths.root), new)
        self.assertFalse(old.exists())
        self.assertEqual((new / "marker").read_text(encoding="utf-8"), "kept")

    def test_both_default_directories_use_new_and_warn_once(self) -> None:
        new = self.home / ".naij"
        old = self.home / ".nitro-cli"
        new.mkdir()
        old.mkdir()
        (old / "marker").write_text("untouched", encoding="utf-8")
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            first = state.resolve_state_paths()
            second = state.resolve_state_paths()

        self.assertEqual(first, second)
        self.assertEqual(Path(first.root), new)
        self.assertEqual((old / "marker").read_text(encoding="utf-8"), "untouched")
        self.assertEqual(stderr.getvalue().count("both"), 1)

    def test_failed_migration_uses_old_directory_without_partial_new_one(self) -> None:
        old = self.home / ".nitro-cli"
        old.mkdir()
        stderr = io.StringIO()

        with patch.object(state.os, "replace", side_effect=OSError("denied")):
            with contextlib.redirect_stderr(stderr):
                paths = state.resolve_state_paths()

        self.assertEqual(Path(paths.root), old)
        self.assertTrue(old.is_dir())
        self.assertFalse((self.home / ".naij").exists())
        self.assertIn("move it manually", stderr.getvalue())

    def test_explicit_canonical_state_dir_disables_default_migration(self) -> None:
        old = self.home / ".nitro-cli"
        old.mkdir()
        explicit = self.set_state_root("explicit")

        paths = state.resolve_state_paths()

        self.assertEqual(Path(paths.root), explicit)
        self.assertTrue(old.is_dir())
        self.assertFalse((self.home / ".naij").exists())

    def test_canonical_state_dir_beats_legacy_state_dir(self) -> None:
        canonical = self.home / "canonical"
        legacy = self.home / "legacy"
        os.environ.update(
            {"NAIJ_STATE_DIR": str(canonical), "NITRO_STATE_DIR": str(legacy)}
        )
        state.reset_state_paths()

        self.assertEqual(Path(state.resolve_state_paths().root), canonical)

    def test_cli_state_dir_beats_canonical_and_disables_migration(self) -> None:
        old = self.home / ".nitro-cli"
        old.mkdir()
        os.environ["NAIJ_STATE_DIR"] = str(self.home / "environment")
        target = self.home / "cli"

        state.configure_state_dir(str(target))

        self.assertEqual(Path(state.resolve_state_paths().root), target)
        self.assertTrue(old.is_dir())
        self.assertFalse((self.home / ".naij").exists())

    def test_state_files_are_private_and_writes_are_atomic(self) -> None:
        root = self.set_state_root()

        state.save_state({"access_token": "secret"})
        state.save_context({"contest": {"org": "o", "comp": "c"}})
        history = Path(state.prepare_history())

        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        for path in (root / "state.json", root / "context.json", history):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads((root / "state.json").read_text()), {"access_token": "secret"})
        self.assertFalse(any(item.name.startswith(".naij-") for item in root.iterdir()))

    def test_failed_atomic_replace_preserves_original_and_removes_temporary(self) -> None:
        root = self.set_state_root()
        root.mkdir()
        target = root / "context.json"
        target.write_bytes(b"old")

        with patch.object(state.os, "replace", side_effect=OSError("denied")):
            with self.assertRaises(OSError):
                state.atomic_write(str(target), b"new")

        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(any(item.name.startswith(".naij-") for item in root.iterdir()))

    def test_corrupt_credentials_raise_actionable_error_without_rewrite(self) -> None:
        root = self.set_state_root()
        root.mkdir()
        credentials = root / "state.json"
        original = b"{ definitely not json"
        credentials.write_bytes(original)

        with self.assertRaisesRegex(state.CredentialsError, r"naij login"):
            state.load_state()

        self.assertEqual(credentials.read_bytes(), original)

    def test_corrupt_context_is_ignored_warned_once_and_not_rewritten(self) -> None:
        root = self.set_state_root()
        root.mkdir()
        context_path = root / "context.json"
        original = b"[not valid context]"
        context_path.write_bytes(original)
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            self.assertEqual(state.load_context(), {})
            self.assertEqual(state.load_context(), {})

        self.assertEqual(context_path.read_bytes(), original)
        self.assertEqual(stderr.getvalue().count("ignoring corrupt context"), 1)

    def test_context_changes_invalidate_only_dependent_selection(self) -> None:
        self.set_state_root()
        first = {"organizationSlug": "org", "competitionSlug": "one"}
        same = {**first, "title": "updated"}
        second = {"organizationSlug": "org", "competitionSlug": "two"}

        state.set_contest(first)
        state.set_task({"id": 1, "title": "Task"})
        state.set_submission("submission-one")
        state.set_contest(same)
        unchanged = state.load_context()
        self.assertEqual(state.selected_task(unchanged), "1")
        self.assertEqual(state.selected_submission(unchanged), "submission-one")

        state.set_task({"id": "2"})
        changed_task = state.load_context()
        self.assertEqual(state.selected_task(changed_task), "2")
        self.assertIsNone(state.selected_submission(changed_task))

        state.set_submission("submission-two")
        state.set_contest(second)
        changed_contest = state.load_context()
        self.assertEqual(state.selected_contest(changed_contest), ("org", "two"))
        self.assertIsNone(state.selected_task(changed_contest))
        self.assertIsNone(state.selected_submission(changed_contest))

    def test_clear_context_retains_offline_cache(self) -> None:
        self.set_state_root()
        state.save_context(
            {
                "contest": {"org": "org", "comp": "contest"},
                "task": {"id": "1"},
                "cache": {"contests": {"all": [{"org": "org", "comp": "contest"}]}},
            }
        )

        state.clear_context()

        context = state.load_context()
        self.assertNotIn("contest", context)
        self.assertNotIn("task", context)
        self.assertIn("cache", context)

    def test_explicit_empty_context_does_not_fall_back_to_disk(self) -> None:
        with patch.object(state, "load_context", side_effect=AssertionError("disk read")):
            self.assertIsNone(state.selected_contest({}))
            self.assertIsNone(state.selected_task({}))
            self.assertIsNone(state.selected_submission({}))


class CompletionTests(unittest.TestCase):
    def context(self) -> dict[str, object]:
        return {
            "contest": {"organizationSlug": "Acme", "competitionSlug": "Open"},
            "task": {"id": "TaskOne"},
            "cache": {
                "contests": {
                    "all": [
                        {"organizationSlug": "Acme", "competitionSlug": "Open"},
                        {"organizationSlug": "Beta", "competitionSlug": "Cup"},
                    ]
                },
                "tasks": {"Acme/Open": [{"id": "TaskOne"}, {"id": 2}]},
                "submissions": {
                    "Acme/Open/TaskOne": [{"id": "ABC-123"}, {"id": "def-456"}]
                },
            },
        }

    def test_static_completion_works_with_empty_cache(self) -> None:
        self.assertIn("completion", completion.candidates(["co"], {}))
        self.assertIn("--api-url", completion.candidates(["--a"], {}))

    def test_cached_completion_is_case_insensitive_and_slash_aware(self) -> None:
        context = self.context()
        self.assertEqual(completion.candidates(["use", "ACME/"] , context), ["Acme/Open"])
        self.assertIn("TaskOne", completion.candidates(["submit", "task"], context))
        self.assertIn("ABC-123", completion.candidates(["submission", "abc"], context))
        self.assertEqual(
            completion.candidates(["submissions", "--mode", "COMP"], context),
            ["complete"],
        )

    def test_completion_with_supplied_context_performs_no_state_or_network_io(self) -> None:
        context = self.context()
        with patch.object(completion, "load_context", side_effect=AssertionError("state")):
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                self.assertEqual(
                    completion.candidates(["use", "beta/"], context), ["Beta/Cup"]
                )

    def test_path_completion_is_local_and_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Result.CSV").write_text("", encoding="utf-8")
            (root / "Sources").mkdir()
            old_cwd = os.getcwd()
            try:
                os.chdir(root)
                self.assertIn(
                    "Result.CSV", completion.candidates(["submit", "-o", "res"], {})
                )
                self.assertIn(
                    "Sources/", completion.candidates(["submit", "-s", "sou"], {})
                )
            finally:
                os.chdir(old_cwd)

    def test_generated_scripts_register_both_names_but_call_naij(self) -> None:
        for shell in ("zsh", "bash", "fish"):
            with self.subTest(shell=shell):
                generated = completion.script(shell)
                self.assertIn("naij", generated)
                self.assertIn("nitro-cli", generated)
                self.assertIn("naij __complete", generated)
                self.assertNotIn("nitro-cli __complete", generated)

    def test_unknown_completion_shell_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported shell"):
            completion.script("powershell")


if __name__ == "__main__":
    unittest.main()

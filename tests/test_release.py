from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nitro_ai_judge_cli import play
from nitro_ai_judge_cli.play_protocol import (
    API_VERSION,
    DEFAULT_MANAGER_IMAGE,
    MANAGER_VERSION,
    MINIMUM_CLI_VERSION,
)


class ReleaseTests(unittest.TestCase):
    def test_3_0_2_versions_keep_protocol_compatibility(self) -> None:
        pyproject = Path(__file__).parents[1].joinpath("pyproject.toml").read_text()
        self.assertIn('version = "3.0.2"', pyproject)
        self.assertEqual(MANAGER_VERSION, "3.0.2")
        self.assertEqual(
            DEFAULT_MANAGER_IMAGE,
            "ghcr.io/mihneateodorstoica/naij-play-manager:3.0.2",
        )
        self.assertEqual(API_VERSION, 1)
        self.assertEqual(MINIMUM_CLI_VERSION, "3.0.0")

    def test_manager_update_advances_only_older_official_images(self) -> None:
        cases = (
            (
                "ghcr.io/mihneateodorstoica/naij-play-manager:3.0.1",
                None,
                DEFAULT_MANAGER_IMAGE,
            ),
            ("naij-play-manager:dev", None, "naij-play-manager:dev"),
            ("registry.example/manager:2.0.0", None, "registry.example/manager:2.0.0"),
            (
                "ghcr.io/mihneateodorstoica/naij-play-manager:3.0.1",
                "naij-play-manager:override",
                "naij-play-manager:override",
            ),
        )
        for configured, explicit, expected in cases:
            with self.subTest(configured=configured, explicit=explicit):
                args = SimpleNamespace(
                    manager_action="update",
                    bind=None,
                    port=None,
                    image=explicit,
                    tls_cert=None,
                    tls_key=None,
                    public_url=None,
                )
                with (
                    patch.object(
                        play,
                        "load_manager_config",
                        return_value={"image": configured},
                    ),
                    patch.object(
                        play,
                        "install_manager",
                        return_value={"manager_version": "3.0.2"},
                    ) as install,
                    patch("builtins.print"),
                ):
                    self.assertEqual(play.cmd_manager(args), 0)
                self.assertEqual(install.call_args.kwargs["image"], expected)


if __name__ == "__main__":
    unittest.main()

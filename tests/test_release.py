from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nitro_ai_judge_cli import play
from nitro_ai_judge_cli.manager.backend import (
    FALLBACK_IMAGES,
    V3_0_2_FALLBACK_IMAGES,
)
from nitro_ai_judge_cli.play_protocol import (
    API_VERSION,
    DEFAULT_MANAGER_IMAGE,
    MANAGER_VERSION,
    MINIMUM_CLI_VERSION,
)


class ReleaseTests(unittest.TestCase):
    def test_3_1_3_versions_keep_protocol_compatibility(self) -> None:
        pyproject = Path(__file__).parents[1].joinpath("pyproject.toml").read_text()
        self.assertIn('version = "3.1.3"', pyproject)
        self.assertEqual(MANAGER_VERSION, "3.1.3")
        self.assertEqual(
            DEFAULT_MANAGER_IMAGE,
            "ghcr.io/mihneateodorstoica/naij-play-manager:3.1.3",
        )
        self.assertEqual(API_VERSION, 1)
        self.assertEqual(MINIMUM_CLI_VERSION, "3.0.0")
        self.assertEqual(
            FALLBACK_IMAGES,
            (
                "ghcr.io/mihneateodorstoica/nitro-contestant-notebook@sha256:d683327e259d4f1fa9a40203295269b3009a18e8a6d0274e17685efd0e9e3ee0",
                "ghcr.io/mihneateodorstoica/nitro-submission-proxy@sha256:57fb32ae07fd6a231a317796508fa05b3d9902f1b2a2ee1be4937a5f85e39bea",
            ),
        )
        self.assertEqual(
            V3_0_2_FALLBACK_IMAGES,
            (
                "nitroai/nitro-test-notebook@sha256:9dd89d1c276b550c1c9bf05b7cf60761996a3dec0bc3a013400221416d8ec22e",
                "nitroai/nitro-test-judge-proxy@sha256:46542d51497d689b7d57acf85b143dc52e4022246afedae0d04dc1325358fd24",
            ),
        )

    def test_manager_install_and_update_advance_only_older_official_images(self) -> None:
        cases = (
            (
                "update",
                "ghcr.io/mihneateodorstoica/naij-play-manager:3.0.3",
                None,
                DEFAULT_MANAGER_IMAGE,
                True,
            ),
            (
                "install",
                "ghcr.io/mihneateodorstoica/naij-play-manager:3.0.3",
                None,
                DEFAULT_MANAGER_IMAGE,
                True,
            ),
            (
                "install",
                DEFAULT_MANAGER_IMAGE,
                None,
                DEFAULT_MANAGER_IMAGE,
                False,
            ),
            (
                "update",
                "ghcr.io/mihneateodorstoica/naij-play-manager:3.1.0",
                None,
                DEFAULT_MANAGER_IMAGE,
                True,
            ),
            (
                "update",
                "ghcr.io/mihneateodorstoica/naij-play-manager@sha256:" + "1" * 64,
                None,
                "ghcr.io/mihneateodorstoica/naij-play-manager@sha256:" + "1" * 64,
                True,
            ),
            (
                "update",
                "ghcr.io/mihneateodorstoica/naij-play-manager:3.0.3-dev",
                None,
                "ghcr.io/mihneateodorstoica/naij-play-manager:3.0.3-dev",
                True,
            ),
            ("install", "naij-play-manager:dev", None, "naij-play-manager:dev", False),
            (
                "update",
                "ghcr.io/mihneateodorstoica/naij-play-manager:3.0.1",
                "naij-play-manager:override",
                "naij-play-manager:override",
                True,
            ),
        )
        for action, configured, explicit, expected, updates in cases:
            with self.subTest(action=action, configured=configured, explicit=explicit):
                args = SimpleNamespace(
                    manager_action=action,
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
                        return_value={"manager_version": "3.1.3"},
                    ) as install,
                    patch("builtins.print"),
                ):
                    self.assertEqual(play.cmd_manager(args), 0)
                self.assertEqual(install.call_args.kwargs["image"], expected)
                self.assertEqual(install.call_args.kwargs["update"], updates)


if __name__ == "__main__":
    unittest.main()

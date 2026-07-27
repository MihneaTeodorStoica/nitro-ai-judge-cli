from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from nitro_ai_judge_cli import play_manager_lifecycle as lifecycle, state
from nitro_ai_judge_cli.play_manager_client import ManagerClient


@unittest.skipUnless(
    os.environ.get("NAIJ_DOCKER_INTEGRATION") == "1",
    "set NAIJ_DOCKER_INTEGRATION=1 to run Docker integration tests",
)
class ManagerInstallIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        state.configure_state_dir(cls.temporary.name)
        cls.image = os.environ.get("NAIJ_PLAY_MANAGER_IMAGE", "naij-play-manager:dev")
        cls.original_names = (
            lifecycle.MANAGER_PROJECT,
            lifecycle.MANAGER_NETWORK,
            lifecycle.MANAGER_VOLUME,
        )
        suffix = uuid.uuid4().hex[:12]
        lifecycle.MANAGER_PROJECT = f"naij-play-manager-test-{suffix}"
        lifecycle.MANAGER_NETWORK = f"naij-play-test-{suffix}"
        lifecycle.MANAGER_VOLUME = f"naij-play-manager-state-test-{suffix}"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            lifecycle.purge_manager_state(force=True)
        finally:
            (
                lifecycle.MANAGER_PROJECT,
                lifecycle.MANAGER_NETWORK,
                lifecycle.MANAGER_VOLUME,
            ) = cls.original_names
            state.configure_state_dir(None)
            cls.temporary.cleanup()

    def test_install_restart_uninstall_preserves_private_volume_and_config(self) -> None:
        info = lifecycle.install_manager(
            bind="127.0.0.1", port=self.port, image=self.image
        )
        self.assertEqual(info["identity"], "naij-play-manager")
        self.assertEqual(ManagerClient.from_state().health()["status"], "healthy")
        paths = lifecycle.manager_paths()
        self.assertEqual(os.stat(paths["token"]).st_mode & 0o777, 0o600)
        with open(paths["compose"], encoding="utf-8") as stream:
            compose = json.load(stream)
        self.assertNotIn(ManagerClient.from_state().token, json.dumps(compose))
        self.assertIn("/var/run/docker.sock", json.dumps(compose))

        lifecycle.manager_compose_action("restart")
        self.assertEqual(lifecycle.manager_status()["health"]["status"], "healthy")
        lifecycle.uninstall_manager()
        self.assertTrue(os.path.exists(paths["config"]))
        volume = subprocess.run(
            ["docker", "volume", "inspect", lifecycle.MANAGER_VOLUME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(volume.returncode, 0)


if __name__ == "__main__":
    unittest.main()

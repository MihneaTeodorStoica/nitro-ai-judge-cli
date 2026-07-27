from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from nitro_ai_judge_cli import play_manager_lifecycle as lifecycle, state
from nitro_ai_judge_cli.play_manager_client import ManagerClient

RUN_DOCKER_INTEGRATION = os.environ.get("NAIJ_DOCKER_INTEGRATION") == "1"
if RUN_DOCKER_INTEGRATION:
    from nitro_ai_judge_cli.manager.backend import DockerBackend


@unittest.skipUnless(
    RUN_DOCKER_INTEGRATION,
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

    @staticmethod
    def _build_service_image(base: str, tag: str, port: int) -> None:
        server = (
            "from http.server import BaseHTTPRequestHandler,HTTPServer;"
            "import json;"
            "print('token=integration-secret',flush=True);"
            "H=type('H',(BaseHTTPRequestHandler,),{"
            "'do_GET':lambda s:(s.send_response(200),s.send_header('Content-Type','application/json'),s.end_headers(),s.wfile.write(b'{}'))[-1],"
            "'log_message':lambda *a:None});"
            f"HTTPServer(('0.0.0.0',{port}),H).serve_forever()"
        )
        dockerfile = f"FROM {base}\nENTRYPOINT {json.dumps(['python', '-u', '-c', server])}\n"
        subprocess.run(
            ["docker", "build", "-t", tag, "-f", "-", "."],
            input=dockerfile,
            text=True,
            check=True,
            stdout=subprocess.DEVNULL,
        )

    @staticmethod
    def _sse_event(stream) -> str:
        event = ""
        while True:
            line = stream.readline().decode().strip()
            if not line:
                if event:
                    return event
                continue
            if line.startswith("event: "):
                event = line.removeprefix("event: ")

    @staticmethod
    def _wait_cached(
        client: ManagerClient, reference: str, workspace_state: str, timeout: float = 15
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            values = client._request(
                "GET", "/nitro/api/v1/competitions?cached=true"
            ).get("competitions", [])
            snapshot = next(
                (item for item in values if item.get("reference") == reference), None
            )
            if snapshot and snapshot.get("workspace_state") == workspace_state:
                return snapshot
            time.sleep(0.2)
        raise AssertionError(f"{reference} did not become {workspace_state} in cached state")

    def test_live_sse_cli_and_raw_docker_synchronization(self) -> None:
        lifecycle.install_manager(bind="127.0.0.1", port=self.port, image=self.image)
        client = ManagerClient.from_state()
        org, competition = "integration", "live-sync"
        reference = f"{org}/{competition}"
        notebook, proxy = DockerBackend.image_names(org, competition)
        self._build_service_image(self.image, notebook, 8888)
        self._build_service_image(self.image, proxy, 9000)
        request = urllib.request.Request(
            f"{client.base_url}/nitro/api/v1/events",
            headers={"Authorization": f"Bearer {client.token}"},
        )
        stream = urllib.request.urlopen(request, timeout=30)
        try:
            self.assertEqual(self._sse_event(stream), "sync")
            environment = {**os.environ, "NAIJ_STATE_DIR": self.temporary.name}
            command = [
                sys.executable,
                "-m",
                "nitro_ai_judge_cli",
                "play",
                "play",
                reference,
                "--no-gpu",
                "--pull",
                "never",
                "--wait-timeout",
                "30",
            ]
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(self._sse_event(stream), "refresh")
            appeared = client._request(
                "GET", "/nitro/api/v1/competitions?cached=true"
            )["competitions"]
            self.assertTrue(any(item["reference"] == reference for item in appeared))
            output, _ = process.communicate(timeout=90)
            self.assertEqual(process.returncode, 0, output)
            self._wait_cached(client, reference, "running")

            headers = {"Authorization": f"Bearer {client.token}"}
            for path in (
                f"/nitro/competitions/{org}/{competition}/jupyter/api",
                f"/nitro/competitions/{org}/{competition}/proxy/health",
            ):
                response = urllib.request.urlopen(
                    urllib.request.Request(client.base_url + path, headers=headers),
                    timeout=10,
                )
                self.assertEqual(response.status, 200)
            self.assertNotIn(
                "integration-secret", client.logs(org, competition, tail=20)["logs"]
            )

            containers = subprocess.check_output(
                [
                    "docker",
                    "ps",
                    "-q",
                    "--filter",
                    f"label=org.nitro-ai.naij.play.identity={reference}",
                ],
                text=True,
            ).split()
            self.assertEqual(len(containers), 2)
            subprocess.run(["docker", "stop", *containers], check=True, stdout=subprocess.DEVNULL)
            self._wait_cached(client, reference, "stopped")
            subprocess.run(["docker", "start", *containers], check=True, stdout=subprocess.DEVNULL)
            self._wait_cached(client, reference, "running")
        finally:
            stream.close()
            try:
                accepted = client.action(
                    org, competition, "delete-workspace", force=True
                )
                client.wait_operation(accepted["operation_id"], timeout=60)
            except Exception:
                pass
            subprocess.run(
                ["docker", "image", "rm", "-f", notebook, proxy],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    unittest.main()

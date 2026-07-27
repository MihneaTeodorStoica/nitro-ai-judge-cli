from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

try:
    from nitro_ai_judge_cli.manager.backend import (
        DockerBackend,
        FALLBACK_IMAGES,
        V3_0_2_FALLBACK_IMAGES,
    )
    from nitro_ai_judge_cli.play_protocol import WireError
except ModuleNotFoundError:
    DockerBackend = None
    FALLBACK_IMAGES = ()
    V3_0_2_FALLBACK_IMAGES = ()
    WireError = Exception


NEXT_FALLBACK_IMAGES = (
    "ghcr.io/mihneateodorstoica/nitro-contestant-notebook@sha256:"
    + "1" * 64,
    "ghcr.io/mihneateodorstoica/nitro-submission-proxy@sha256:"
    + "2" * 64,
)


@unittest.skipIf(DockerBackend is None, "aiohttp manager extra is not installed")
class SharedFallbackImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.backend = DockerBackend(self.tempdir.name, "manager:test")

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def saved_3_0_2_backend(self, suffix: str) -> DockerBackend:
        backend = DockerBackend(f"{self.tempdir.name}/{suffix}", "manager:test")
        backend._atomic_compose(
            backend.compose_path("org", "contest"),
            backend._compose(
                "org",
                "contest",
                gpu=False,
                pull_policy="never",
                images=V3_0_2_FALLBACK_IMAGES,
            ),
        )
        return backend

    async def test_competitions_share_pinned_fallback_references(self) -> None:
        cached: set[str] = set()
        primary_attempts: list[str] = []

        async def pull_image(image: str, *_args) -> None:
            if image not in FALLBACK_IMAGES:
                primary_attempts.append(image)
                raise WireError(
                    "operation_failed",
                    "pull access denied: repository does not exist",
                    stage="pulling",
                )
            cached.add(image)

        self.backend._image_present = AsyncMock(
            side_effect=lambda image: image in cached
        )
        self.backend._pull_image = AsyncMock(side_effect=pull_image)
        self.backend.run = AsyncMock(return_value=(0, "", ""))

        first_resolved = await self.backend._pull(
            "org", "first", "missing", AsyncMock()
        )
        self.backend._atomic_compose(
            self.backend.compose_path("org", "first"),
            self.backend._compose(
                "org",
                "first",
                gpu=False,
                pull_policy="never",
                images=first_resolved,
            ),
        )

        first = await self.backend.images("org", "first")
        second = await self.backend.images("org", "second")
        first_images = (first["notebook"]["name"], first["proxy"]["name"])
        second_images = (second["notebook"]["name"], second["proxy"]["name"])

        self.assertEqual(first_images, FALLBACK_IMAGES)
        self.assertEqual(second_images, self.backend.image_names("org", "second"))
        self.assertEqual(second["notebook"]["state"], "missing")
        self.assertEqual(second["proxy"]["state"], "missing")

        second_resolved = await self.backend._pull(
            "org", "second", "missing", AsyncMock()
        )
        self.assertEqual(
            first_resolved,
            FALLBACK_IMAGES,
        )
        self.assertEqual(second_resolved, FALLBACK_IMAGES)
        self.assertEqual(
            primary_attempts,
            [
                *self.backend.image_names("org", "first"),
                *self.backend.image_names("org", "second"),
            ],
        )
        self.assertFalse(
            any(
                call.args[0][:2] == ["docker", "tag"]
                for call in self.backend.run.await_args_list
            )
        )

    async def test_pull_persists_fallback_choice_without_creating_an_environment(self) -> None:
        self.backend._pull = AsyncMock(return_value=FALLBACK_IMAGES)
        self.backend._image_present = AsyncMock(
            side_effect=lambda image: image in FALLBACK_IMAGES
        )
        self.backend.run = AsyncMock(return_value=(1, "", ""))

        result = await self.backend.perform(
            "org", "contest", "pull", {"pull": "missing"}, AsyncMock()
        )
        reloaded = DockerBackend(self.tempdir.name, "manager:test")
        reloaded._image_present = AsyncMock(
            side_effect=lambda image: image in FALLBACK_IMAGES
        )
        reloaded.run = AsyncMock(return_value=(1, "", ""))
        later = await reloaded.inspect_competition("org", "contest")
        untouched = await reloaded.inspect_competition("org", "other")

        self.assertEqual(result["image_state"], "ready")
        self.assertEqual(later["image_state"], "ready")
        self.assertEqual(untouched["image_state"], "missing")
        self.assertEqual(
            tuple(later["images"][role]["name"] for role in ("notebook", "proxy")),
            FALLBACK_IMAGES,
        )
        with self.assertRaises(WireError) as missing_environment:
            await reloaded.perform(
                "org", "contest", "start", {}, AsyncMock()
            )
        self.assertEqual(missing_environment.exception.type, "not_found")

    async def test_saved_v3_aliases_resolve_to_pinned_fallbacks(self) -> None:
        legacy = (
            "naij-fallback/org-contest-notebook:latest",
            "naij-fallback/org-contest-judge-proxy:latest",
        )
        self.backend._atomic_compose(
            self.backend.compose_path("org", "contest"),
            self.backend._compose(
                "org", "contest", gpu=False, pull_policy="never", images=legacy
            ),
        )
        self.backend._image_present = AsyncMock(
            side_effect=lambda image: image in FALLBACK_IMAGES
        )

        images = await self.backend.images("org", "contest")

        self.assertEqual(self.backend._saved_images("org", "contest"), legacy)
        self.assertEqual(
            tuple(images[role]["name"] for role in ("notebook", "proxy")),
            FALLBACK_IMAGES,
        )
        self.assertTrue(all(images[role]["fallback"] for role in images))

    async def test_cached_contest_images_take_precedence_over_saved_fallbacks(self) -> None:
        self.backend._atomic_compose(
            self.backend.compose_path("org", "contest"),
            self.backend._compose(
                "org",
                "contest",
                gpu=False,
                pull_policy="never",
                images=FALLBACK_IMAGES,
            ),
        )
        self.backend._image_present = AsyncMock(return_value=True)

        images = await self.backend.images("org", "contest")

        self.assertEqual(
            tuple(images[role]["name"] for role in ("notebook", "proxy")),
            self.backend.image_names("org", "contest"),
        )
        self.assertFalse(any(images[role]["fallback"] for role in images))

    async def test_start_and_restart_preserve_saved_3_0_2_fallbacks(self) -> None:
        for action in ("start", "restart"):
            with self.subTest(action=action):
                backend = self.saved_3_0_2_backend(action)
                backend._pull = AsyncMock()
                backend.run = AsyncMock(return_value=(0, "", ""))
                backend._wait_services_with_proxy_fallback = AsyncMock()
                backend.inspect_competition = AsyncMock(return_value={})

                with patch(
                    "nitro_ai_judge_cli.manager.backend.FALLBACK_IMAGES",
                    NEXT_FALLBACK_IMAGES,
                ):
                    await backend.perform("org", "contest", action, {}, AsyncMock())

                self.assertEqual(
                    backend._saved_images("org", "contest"),
                    V3_0_2_FALLBACK_IMAGES,
                )
                backend._pull.assert_not_awaited()

    async def test_pull_play_recreate_migrate_only_to_cached_3_0_3_fallbacks(self) -> None:
        for cached in (True, False):
            for action in ("pull", "play", "recreate"):
                with self.subTest(cached=cached, action=action):
                    backend = self.saved_3_0_2_backend(f"{cached}-{action}")
                    backend._image_present = AsyncMock(
                        side_effect=lambda image: cached and image in NEXT_FALLBACK_IMAGES
                    )
                    backend._pull_image = AsyncMock()
                    backend.run = AsyncMock(return_value=(0, "", ""))
                    backend.inspect_competition = AsyncMock(return_value={})
                    backend._prepare_legacy = AsyncMock(
                        return_value={
                            "workspace": backend.names("org", "contest")["workspace"]
                        }
                    )
                    backend._write_volume_file = AsyncMock()
                    backend._gpu_enabled = AsyncMock(return_value=False)
                    backend._wait_services_with_proxy_fallback = AsyncMock()
                    backend._finish_legacy = AsyncMock()

                    with patch(
                        "nitro_ai_judge_cli.manager.backend.FALLBACK_IMAGES",
                        NEXT_FALLBACK_IMAGES,
                    ):
                        operation = backend.perform(
                            "org",
                            "contest",
                            action,
                            {"pull": "never", "gpu": False},
                            AsyncMock(),
                        )
                        if cached:
                            await operation
                        else:
                            with self.assertRaises(WireError) as raised:
                                await operation
                            self.assertEqual(raised.exception.status, 409)

                    self.assertEqual(
                        backend._saved_images("org", "contest"),
                        NEXT_FALLBACK_IMAGES if cached else V3_0_2_FALLBACK_IMAGES,
                    )
                    backend._pull_image.assert_not_awaited()

    async def test_delete_image_never_removes_shared_fallbacks(self) -> None:
        primary = self.backend.image_names("org", "contest")
        self.backend._assert_owned_containers = AsyncMock()
        self.backend._image_present = AsyncMock(return_value=True)
        self.backend.run = AsyncMock(return_value=(0, "", ""))
        self.backend.inspect_competition = AsyncMock(return_value={})

        await self.backend.perform(
            "org", "contest", "delete-image", {}, AsyncMock()
        )

        removed = [
            call.args[0][-1]
            for call in self.backend.run.await_args_list
            if call.args[0][:3] == ["docker", "image", "rm"]
        ]
        self.assertEqual(removed, list(primary))
        self.assertTrue(all(image not in removed for image in FALLBACK_IMAGES))


if __name__ == "__main__":
    unittest.main()

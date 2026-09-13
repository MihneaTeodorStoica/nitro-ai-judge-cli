from __future__ import annotations

import os
import re
import tarfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
LICENSE_FILES = ("LICENSE", "NOTICE", "LICENSES/Inter-OFL-1.1.txt", "LICENSES/Lexend-OFL-1.1.txt")
FONT_FILES = (
    "inter-latin.woff2",
    "inter-latin-ext.woff2",
    "lexend-deca-latin.woff2",
    "lexend-deca-latin-ext.woff2",
)


def project_version() -> str:
    """Read the version without tomllib, which is unavailable on Python 3.10."""
    match = re.search(
        r'^version\s*=\s*"([^"\n]+)"\s*$',
        ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise AssertionError("pyproject.toml must define [project].version")
    return match.group(1)


class PackagingMetadataTests(unittest.TestCase):
    def test_pep_639_metadata_covers_project_and_bundled_fonts(self) -> None:
        pyproject = ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(
            pyproject, re.compile(r'^license\s*=\s*"MIT AND OFL-1\.1"\s*$', re.MULTILINE)
        )
        self.assertRegex(
            pyproject,
            re.compile(
                r'^license-files\s*=\s*\["LICENSE", "NOTICE", "LICENSES/\*\.txt"\]\s*$',
                re.MULTILINE,
            ),
        )

    def test_font_notice_maps_every_bundled_font_to_its_license(self) -> None:
        notice = re.sub(r"\s+", " ", ROOT.joinpath("NOTICE").read_text(encoding="utf-8"))
        for font in FONT_FILES:
            self.assertIn(font, notice)
        self.assertIn("https://github.com/rsms/inter", notice)
        self.assertIn("Copyright (c) 2016 The Inter Project Authors", notice)
        self.assertIn("https://github.com/googlefonts/lexend", notice)
        self.assertIn("Copyright 2018 The Lexend Project Authors", notice)
        self.assertIn("Reserved Font Name “RevReading Lexend”", notice)
        self.assertIn(
            "Copyright (c) 2016 The Inter Project Authors (https://github.com/rsms/inter)",
            ROOT.joinpath("LICENSES/Inter-OFL-1.1.txt").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Copyright 2018 The Lexend Project Authors (https://github.com/googlefonts/lexend)",
            ROOT.joinpath("LICENSES/Lexend-OFL-1.1.txt").read_text(encoding="utf-8"),
        )

    def test_built_distributions_include_license_metadata_and_texts(self) -> None:
        # Distribution inspection is an explicit release check: ordinary unit
        # test runs must not accidentally inspect stale local artifacts.
        if os.environ.get("NAIJ_VERIFY_DIST") != "1":
            self.skipTest("set NAIJ_VERIFY_DIST=1 after building distributions")
        dist = ROOT / "dist"
        version = project_version()
        wheels = list(dist.glob(f"nitro_ai_judge_cli-{version}-*.whl"))
        sdists = list(dist.glob(f"nitro_ai_judge_cli-{version}.tar.gz"))
        if not wheels or not sdists:
            self.fail("NAIJ_VERIFY_DIST=1 requires wheel and sdist in dist/")

        with zipfile.ZipFile(max(wheels, key=lambda path: path.stat().st_mtime)) as wheel:
            names = wheel.namelist()
            metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
            metadata = wheel.read(metadata_name).decode()
            self.assertIn("License-Expression: MIT AND OFL-1.1", metadata)
            for license_file in LICENSE_FILES:
                self.assertIn(f"License-File: {license_file}", metadata)
                self.assertTrue(any(name.endswith(f"licenses/{license_file}") for name in names))
            for font_file in FONT_FILES:
                self.assertIn(f"nitro_ai_judge_cli/manager/assets/{font_file}", names)

        with tarfile.open(max(sdists, key=lambda path: path.stat().st_mtime)) as sdist:
            names = sdist.getnames()
            for license_file in LICENSE_FILES:
                self.assertTrue(any(name.endswith(f"/{license_file}") for name in names))
            for font_file in FONT_FILES:
                self.assertTrue(any(name.endswith(f"/manager/assets/{font_file}") for name in names))


if __name__ == "__main__":
    unittest.main()

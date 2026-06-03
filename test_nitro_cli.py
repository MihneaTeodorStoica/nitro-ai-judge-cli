import os
import unittest
from unittest.mock import patch

import nitro_cli


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.original_api_base_url = nitro_cli.API_BASE_URL
        self.original_submission_proxy_mode = nitro_cli.SUBMISSION_PROXY_MODE

    def tearDown(self):
        nitro_cli.API_BASE_URL = self.original_api_base_url
        nitro_cli.SUBMISSION_PROXY_MODE = self.original_submission_proxy_mode

    def configure_with_env(self, env, api_url=None, submission_proxy=False):
        with patch.dict(os.environ, env, clear=True):
            nitro_cli.configure_runtime(api_url, submission_proxy)
            return nitro_cli.API_BASE_URL, nitro_cli.SUBMISSION_PROXY_MODE

    def test_default_api_url_without_overrides(self):
        api_url, proxy_mode = self.configure_with_env({})
        self.assertEqual(api_url, nitro_cli.DEFAULT_API_BASE_URL)
        self.assertFalse(proxy_mode)

    def test_proxy_url_is_used_as_fallback_and_enables_proxy_mode(self):
        api_url, proxy_mode = self.configure_with_env(
            {"PROXY_URL": " http://proxy.local:9000/ "}
        )
        self.assertEqual(api_url, "http://proxy.local:9000")
        self.assertTrue(proxy_mode)

    def test_nitro_api_base_url_beats_proxy_url(self):
        api_url, proxy_mode = self.configure_with_env(
            {
                "NITRO_API_BASE_URL": " http://api.local:8080/ ",
                "PROXY_URL": "http://proxy.local:9000",
            }
        )
        self.assertEqual(api_url, "http://api.local:8080")
        self.assertFalse(proxy_mode)

    def test_cli_api_url_beats_env_urls(self):
        api_url, proxy_mode = self.configure_with_env(
            {
                "NITRO_API_BASE_URL": "http://api.local:8080",
                "PROXY_URL": "http://proxy.local:9000",
            },
            api_url=" http://cli.local:7777/ ",
        )
        self.assertEqual(api_url, "http://cli.local:7777")
        self.assertFalse(proxy_mode)

    def test_empty_env_values_are_ignored(self):
        api_url, proxy_mode = self.configure_with_env(
            {"NITRO_API_BASE_URL": " ", "PROXY_URL": "\t "}
        )
        self.assertEqual(api_url, nitro_cli.DEFAULT_API_BASE_URL)
        self.assertFalse(proxy_mode)

    def test_proxy_url_does_not_enable_proxy_mode_when_cli_url_is_selected(self):
        api_url, proxy_mode = self.configure_with_env(
            {"PROXY_URL": "http://proxy.local:9000"},
            api_url="http://manual.local:8080",
        )
        self.assertEqual(api_url, "http://manual.local:8080")
        self.assertFalse(proxy_mode)

    def test_submission_proxy_flag_forces_proxy_mode(self):
        api_url, proxy_mode = self.configure_with_env({}, submission_proxy=True)
        self.assertEqual(api_url, nitro_cli.DEFAULT_API_BASE_URL)
        self.assertTrue(proxy_mode)

    def test_submission_proxy_env_forces_proxy_mode(self):
        api_url, proxy_mode = self.configure_with_env(
            {"NITRO_SUBMISSION_PROXY": "yes"}
        )
        self.assertEqual(api_url, nitro_cli.DEFAULT_API_BASE_URL)
        self.assertTrue(proxy_mode)


if __name__ == "__main__":
    unittest.main()

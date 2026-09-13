"""Environment and process-local runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os

from . import __version__


BASE_URL = "https://judge.nitro-ai.org"
DEFAULT_API_BASE_URL = f"{BASE_URL}/api"
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
DEFAULT_PAGE_SIZE = 20
DEFAULT_SUBMISSION_PAGE_SIZE = 10
MAX_PAGINATION_PAGES = 1_000
TASK_FILE_CATEGORIES = {
    "statement": "Statement",
    "train_data": "Train data",
    "test_data": "Test data",
    "sample_output": "Sample output",
    "custom_archive": "Starter kit",
}
DEFAULT_TASK_FILE_CATEGORIES = tuple(TASK_FILE_CATEGORIES)
TASK_FILE_PAGE_LABELS = {
    "train_data": "Train Data",
    "test_data": "Test Data",
    "sample_output": "Sample Output",
    "custom_archive": "Custom Archive",
}
USER_AGENT = f"NAIJ/{__version__}"


def clean_env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def clean_api_url(value: str) -> str:
    return value.strip().rstrip("/")


def env_flag(name: str) -> bool:
    value = clean_env_value(name)
    return value is not None and value.lower() in TRUE_ENV_VALUES


def resolve_api_base_url(api_url: str | None = None) -> tuple[str, bool]:
    if api_url and api_url.strip():
        return clean_api_url(api_url), False
    for name in ("NAIJ_API_BASE_URL", "NITRO_API_BASE_URL"):
        value = clean_env_value(name)
        if value:
            return clean_api_url(value), False
    proxy_url = clean_env_value("PROXY_URL")
    if proxy_url:
        return clean_api_url(proxy_url), True
    return DEFAULT_API_BASE_URL, False


def resolve_submission_proxy(cli_enabled: bool, proxy_url_selected: bool) -> bool:
    if cli_enabled:
        return True
    value = clean_env_value("NAIJ_SUBMISSION_PROXY")
    if value is not None:
        return value.lower() in TRUE_ENV_VALUES
    value = clean_env_value("NITRO_SUBMISSION_PROXY")
    if value is not None:
        return value.lower() in TRUE_ENV_VALUES
    return proxy_url_selected


@dataclass(frozen=True)
class RuntimeConfig:
    api_base_url: str
    submission_proxy: bool
    api_source: str = "programmatic"
    proxy_source: str = "programmatic"

    @classmethod
    def resolve(
        cls, api_url: str | None = None, submission_proxy: bool = False
    ) -> "RuntimeConfig":
        resolved_url, proxy_url_selected = resolve_api_base_url(api_url)
        return cls(
            api_base_url=resolved_url,
            api_source="--api-url" if api_url and api_url.strip() else next((name for name in ("NAIJ_API_BASE_URL", "NITRO_API_BASE_URL", "PROXY_URL") if clean_env_value(name)), "default"),
            proxy_source="--submission-proxy" if submission_proxy else next((name for name in ("NAIJ_SUBMISSION_PROXY", "NITRO_SUBMISSION_PROXY") if clean_env_value(name) is not None), "PROXY_URL" if proxy_url_selected else "default"),
            submission_proxy=resolve_submission_proxy(
                submission_proxy, proxy_url_selected
            ),
        )


_runtime = RuntimeConfig.resolve()


def configure_runtime(
    api_url: str | None = None, submission_proxy: bool = False
) -> RuntimeConfig:
    global _runtime
    _runtime = RuntimeConfig.resolve(api_url, submission_proxy)
    return _runtime


def runtime() -> RuntimeConfig:
    return _runtime

"""Nitro AI Judge CLI."""

from importlib import metadata


def _version_key(value: str) -> tuple[int, ...]:
    parts = value.split(".")
    return tuple(int(part) if part.isdigit() else 0 for part in parts[:3])


try:
    __version__ = max(
        (
            distribution.version
            for distribution in metadata.distributions(name="nitro-ai-judge-cli")
        ),
        key=_version_key,
    )
except (metadata.PackageNotFoundError, ValueError):
    __version__ = "0+unknown"


__all__ = ["__version__"]

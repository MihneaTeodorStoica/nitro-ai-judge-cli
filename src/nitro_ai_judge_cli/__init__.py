"""Nitro AI Judge CLI."""

from importlib import metadata


try:
    __version__ = metadata.version("nitro-ai-judge-cli")
except metadata.PackageNotFoundError:
    __version__ = "0+unknown"


__all__ = ["__version__"]

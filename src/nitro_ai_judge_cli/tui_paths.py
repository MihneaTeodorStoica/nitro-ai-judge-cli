"""Nonblocking, bounded local path suggestions for TUI inputs."""
from __future__ import annotations

import asyncio
import itertools
import os

from textual.suggester import Suggester
from textual.widgets import Input


def complete_path(value: str, *, directories_only: bool = False) -> str | None:
    if not value or "\x00" in value:
        return None
    if value == "~":
        return "~" + os.sep
    expanded = os.path.expanduser(value)
    parent, prefix = os.path.split(expanded)
    typed_name = os.path.basename(value)
    typed_prefix = value[:-len(typed_name)] if typed_name else value
    try:
        with os.scandir(parent or ".") as entries:
            matches = []
            for entry in itertools.islice(entries, 1000):
                if not entry.name.startswith(prefix):
                    continue
                directory = entry.is_dir()
                if directories_only and not directory:
                    continue
                matches.append(entry.name + (os.sep if directory else ""))
        if matches:
            return typed_prefix + sorted(matches)[0]
    except OSError:
        pass
    return None


class PathSuggester(Suggester):
    def __init__(self, *, directories_only: bool = False) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self.directories_only = directories_only

    async def get_suggestion(self, value: str) -> str | None:
        return await asyncio.to_thread(complete_path, value, directories_only=self.directories_only)


class PathInput(Input):
    """Right at end accepts a suggestion; Tab remains normal focus traversal."""
    def __init__(self, *args, directories_only: bool = False, **kwargs) -> None:
        super().__init__(*args, suggester=PathSuggester(directories_only=directories_only), **kwargs)

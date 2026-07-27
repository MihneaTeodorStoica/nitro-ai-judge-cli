"""Shared terminal formatting and progress output."""

from __future__ import annotations

import sys
import threading
import time
from typing import TextIO, Any


SPINNER_FRAMES = "|/-\\"
SPINNER_INTERVAL_SECONDS = 0.12


class Spinner:
    def __init__(self, label: str, *, stream: TextIO = sys.stdout) -> None:
        self.label = label
        self.stream = stream
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.tty = bool(getattr(stream, "isatty", lambda: False)())

    def start(self) -> "Spinner":
        if not self.tty:
            print(self.label, file=self.stream, flush=True)
            return self

        def run() -> None:
            index = 0
            while not self.stop_event.is_set():
                self.stream.write(f"\r{self.label} {SPINNER_FRAMES[index]}")
                self.stream.flush()
                index = (index + 1) % len(SPINNER_FRAMES)
                self.stop_event.wait(SPINNER_INTERVAL_SECONDS)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def update(self, label: str) -> None:
        self.label = label

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        if self.tty:
            self.stream.write("\r\033[K")
            self.stream.flush()


def _start_spinner(prefix: str) -> tuple[threading.Event, threading.Thread]:
    """Compatibility helper for existing download progress code."""
    stop_event = threading.Event()

    def run() -> None:
        index = 0
        tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        if not tty:
            print(prefix, flush=True)
            return
        while not stop_event.is_set():
            sys.stdout.write(f"\r{prefix} {SPINNER_FRAMES[index]}")
            sys.stdout.flush()
            index = (index + 1) % len(SPINNER_FRAMES)
            stop_event.wait(SPINNER_INTERVAL_SECONDS)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return stop_event, thread


def _stop_spinner(stop_event: threading.Event, thread: threading.Thread) -> None:
    stop_event.set()
    thread.join()
    if bool(getattr(sys.stdout, "isatty", lambda: False)()):
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


def format_datetime_ms(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value / 1000))

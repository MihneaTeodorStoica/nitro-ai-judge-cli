"""Bounded-memory, replayable transfer primitives."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import tempfile
import uuid

CHUNK = 64 * 1024
ERROR_LIMIT = 64 * 1024
RESPONSE_LIMIT = 32 * 1024 * 1024


def receive(response, status: int, headers: dict, output=None) -> bytes:
    first = response.read(CHUNK)
    html = any(k.lower() == "content-type" and "text/html" in v.lower() for k, v in headers.items()) or first.lstrip().lower().startswith((b"<!doctype html", b"<html"))
    if status == 200 and output is not None and not html:
        expected = next((int(v) for k, v in headers.items() if k.lower() == "content-length" and v.isdigit()), None)
        count = len(first)
        output.write(first)
        while chunk := response.read(CHUNK):
            count += len(chunk)
            output.write(chunk)
        if expected is not None and count != expected:
            raise OSError("Incomplete download (Content-Length mismatch)")
        return b""
    limit = ERROR_LIMIT if status >= 300 or (html and output is not None) else RESPONSE_LIMIT
    parts, size = [first[:limit]], len(first)
    while size < limit:
        chunk = response.read(min(CHUNK, limit - size))
        if not chunk:
            break
        parts.append(chunk)
        size += len(chunk)
    if status < 300 and output is None and size == limit and response.read(1):
        raise OSError("Response too large; use the streaming download interface")
    return b"".join(parts)


class Multipart:
    """A new iterator reopens files for each same-origin request replay."""
    def __init__(self, fields: dict[str, str], files: dict[str, tuple[str, str]]) -> None:
        self.boundary = "----NAIJ" + uuid.uuid4().hex
        self.parts = []
        self.length = 0
        for name, value in fields.items():
            self._literal(f'--{self.boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        for name, (path, content_type) in files.items():
            filename = Path(path).name.replace('"', "_").replace("\r", "_").replace("\n", "_")
            self._literal(f'--{self.boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode())
            metadata = os.stat(path)
            self.parts.append((path, metadata.st_size, metadata.st_mtime_ns))
            self.length += metadata.st_size
            self._literal(b"\r\n")
        self._literal(f"--{self.boundary}--\r\n".encode())

    def _literal(self, value):
        self.parts.append(value)
        self.length += len(value)

    def __iter__(self):
        for part in self.parts:
            if isinstance(part, bytes):
                yield part
                continue
            path, size, mtime = part
            with open(path, "rb") as stream:
                stat = os.fstat(stream.fileno())
                if (stat.st_size, stat.st_mtime_ns) != (size, mtime):
                    raise OSError("Upload file changed before replay")
                remaining = size
                while remaining:
                    chunk = stream.read(min(CHUNK, remaining))
                    if not chunk:
                        raise OSError("Upload file changed during transfer")
                    remaining -= len(chunk)
                    yield chunk


def atomic_copy(source, target: str, *, force: bool = False) -> None:
    """Stage in the destination directory; never leave a partial destination."""
    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    if os.path.lexists(target) and not force:
        raise RuntimeError(f"Refusing to overwrite existing file: {target}")
    fd, temporary = tempfile.mkstemp(prefix=".naij-download-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as out:
            source.seek(0)
            shutil.copyfileobj(source, out, CHUNK)
            out.flush()
            os.fsync(out.fileno())
        if force:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise RuntimeError(f"Refusing to overwrite existing file: {target}") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

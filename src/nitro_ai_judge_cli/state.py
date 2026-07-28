"""Private, atomic credential and persistent-context storage."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import sys
import tempfile
from typing import Any, Callable, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .config import clean_env_value


class CredentialsError(RuntimeError):
    """The saved credential file exists but cannot safely be loaded."""


@dataclass(frozen=True)
class StatePaths:
    root: str

    @property
    def credentials(self) -> str:
        return os.path.join(self.root, "state.json")

    @property
    def context(self) -> str:
        return os.path.join(self.root, "context.json")

    @property
    def history(self) -> str:
        return os.path.join(self.root, "history")

    @property
    def play(self) -> str:
        return os.path.join(self.root, "contestant-cloud")

    @property
    def play_manager(self) -> str:
        return os.path.join(self.root, "play-manager")


_warned: set[str] = set()
_cli_state_dir: str | None = None
_cached_key: tuple[str | None, str | None, str | None, str] | None = None
_cached_paths: StatePaths | None = None


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    print(f"warning: {message}", file=sys.stderr)


def _default_roots() -> tuple[str, str]:
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".naij"), os.path.join(home, ".nitro-cli")


def _harden_existing(paths: StatePaths) -> None:
    if os.path.isdir(paths.root):
        try:
            os.chmod(paths.root, 0o700)
        except OSError:
            pass
    for path in (paths.credentials, paths.context, paths.history):
        if os.path.isfile(path):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass


def resolve_state_paths() -> StatePaths:
    global _cached_key, _cached_paths
    canonical_env = clean_env_value("NAIJ_STATE_DIR")
    legacy_env = clean_env_value("NITRO_STATE_DIR")
    canonical, legacy = _default_roots()
    key = (_cli_state_dir, canonical_env, legacy_env, canonical)
    if key == _cached_key and _cached_paths is not None:
        return _cached_paths

    if _cli_state_dir:
        root = _cli_state_dir
    elif canonical_env:
        root = os.path.abspath(os.path.expanduser(canonical_env))
    elif legacy_env:
        root = os.path.abspath(os.path.expanduser(legacy_env))
    elif os.path.exists(canonical):
        root = canonical
        if os.path.exists(legacy):
            _warn_once(
                "both-state-roots",
                f"both {canonical} and {legacy} exist; using {canonical} and leaving the old directory untouched",
            )
    elif os.path.exists(legacy):
        try:
            os.replace(legacy, canonical)
            root = canonical
        except OSError as exc:
            root = legacy
            _warn_once(
                "state-migration-failed",
                f"could not rename {legacy} to {canonical} ({exc}); using the old directory for this run; move it manually when convenient",
            )
    else:
        root = canonical

    _cached_key = key
    _cached_paths = StatePaths(root)
    _harden_existing(_cached_paths)
    return _cached_paths


def reset_state_paths() -> None:
    """Forget process-local path resolution (primarily useful to isolated callers)."""
    global _cached_key, _cached_paths
    _cached_key = None
    _cached_paths = None


def configure_state_dir(path: str | None) -> None:
    """Set or clear the process-local CLI state-directory override."""
    global _cli_state_dir
    _cli_state_dir = (
        os.path.abspath(os.path.expanduser(path.strip()))
        if path and path.strip()
        else None
    )
    reset_state_paths()


def ensure_state_dir(paths: StatePaths | None = None) -> StatePaths:
    paths = paths or resolve_state_paths()
    os.makedirs(paths.root, mode=0o700, exist_ok=True)
    os.chmod(paths.root, 0o700)
    return paths


def _fsync_directory(path: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".naij-", dir=parent)
        with os.fdopen(descriptor, "wb") as stream:
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), mode)
            else:
                os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _fsync_directory(parent)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _write_json(path: str, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write(path, payload)


def load_state() -> dict[str, Any] | None:
    path = resolve_state_paths().credentials
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("top-level value is not an object")
        os.chmod(path, 0o600)
        return value
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CredentialsError(
            f"saved credentials in {path} are corrupt or unreadable ({exc}); move the file aside, then run `naij login`"
        ) from exc


def save_state(value: dict[str, Any]) -> None:
    paths = ensure_state_dir()
    _write_json(paths.credentials, value)


def clear_credentials() -> bool:
    """Remove only the credential file for the resolved state root."""
    path = resolve_state_paths().credentials
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CredentialsError(
            f"could not remove saved credentials in {path} ({exc})"
        ) from exc
    _fsync_directory(os.path.dirname(path))
    return True


def load_context() -> dict[str, Any]:
    path = resolve_state_paths().context
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("top-level value is not an object")
        os.chmod(path, 0o600)
        return value
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _warn_once(
            "corrupt-context",
            f"ignoring corrupt context file {path} ({exc}); run `naij use --clear` to replace it",
        )
        return {}


def _save_context_unlocked(value: dict[str, Any]) -> None:
    paths = ensure_state_dir()
    _write_json(paths.context, value)


@contextmanager
def _context_lock() -> Iterator[None]:
    paths = ensure_state_dir()
    path = f"{paths.context}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as lock:
        if os.name == "nt":
            if os.path.getsize(path) == 0:
                lock.write(b"\0")
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def save_context(value: dict[str, Any]) -> None:
    with _context_lock():
        _save_context_unlocked(value)


def mutate_context(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _context_lock():
        context = load_context()
        change(context)
        _save_context_unlocked(context)
        return context


def clear_context() -> None:
    def clear(context: dict[str, Any]) -> None:
        cache = context.get("cache")
        context.clear()
        if isinstance(cache, dict):
            context["cache"] = cache

    mutate_context(clear)


def selected_contest(context: dict[str, Any] | None = None) -> tuple[str, str] | None:
    contest = (load_context() if context is None else context).get("contest")
    if not isinstance(contest, dict):
        return None
    org = contest.get("organizationSlug") or contest.get("org")
    comp = contest.get("competitionSlug") or contest.get("comp")
    if not org or not comp:
        return None
    return str(org), str(comp)


def selected_task(context: dict[str, Any] | None = None) -> str | None:
    task = (load_context() if context is None else context).get("task")
    if isinstance(task, dict) and task.get("id") is not None:
        return str(task["id"])
    if isinstance(task, (str, int)):
        return str(task)
    return None


def selected_task_number(context: dict[str, Any] | None = None) -> str | None:
    context = load_context() if context is None else context
    task_id = selected_task(context)
    contest = selected_contest(context)
    if task_id is None or contest is None:
        return task_id
    cache = context.get("cache", {})
    bucket = cache.get("tasks", {}) if isinstance(cache, dict) else {}
    items = bucket.get(f"{contest[0]}/{contest[1]}", []) if isinstance(bucket, dict) else []
    for number, item in enumerate(items if isinstance(items, list) else [], 1):
        if isinstance(item, dict) and str(item.get("id")) == task_id:
            return str(number)
    return task_id


def selected_submission(context: dict[str, Any] | None = None) -> str | None:
    submission = (load_context() if context is None else context).get("submission")
    if isinstance(submission, dict) and submission.get("id"):
        return str(submission["id"])
    if isinstance(submission, str):
        return submission
    return None


def set_contest(contest: dict[str, Any]) -> dict[str, Any]:
    org = str(contest.get("organizationSlug") or contest.get("org") or "")
    comp = str(contest.get("competitionSlug") or contest.get("comp") or "")
    if not org or not comp:
        raise ValueError("contest requires organizationSlug and competitionSlug")

    def select(context: dict[str, Any]) -> None:
        previous = selected_contest(context)
        context["contest"] = {
            **contest,
            "organizationSlug": org,
            "competitionSlug": comp,
        }
        if previous != (org, comp):
            context.pop("task", None)
            context.pop("submission", None)

    return mutate_context(select)


def set_task(task: dict[str, Any] | str | int) -> dict[str, Any]:
    task_value = {"id": str(task)} if not isinstance(task, dict) else dict(task)
    if task_value.get("id") is None:
        raise ValueError("task requires an id")
    task_value["id"] = str(task_value["id"])

    def select(context: dict[str, Any]) -> None:
        if selected_task(context) != task_value["id"]:
            context.pop("submission", None)
        context["task"] = task_value

    return mutate_context(select)


def set_submission(submission: dict[str, Any] | str) -> dict[str, Any]:
    value = {"id": submission} if isinstance(submission, str) else dict(submission)

    return mutate_context(lambda context: context.__setitem__("submission", value))


def update_cache(kind: str, key: str, items: list[dict[str, Any]]) -> None:
    def update(context: dict[str, Any]) -> None:
        cache = context.setdefault("cache", {})
        if not isinstance(cache, dict):
            cache = {}
            context["cache"] = cache
        bucket = cache.setdefault(kind, {})
        if not isinstance(bucket, dict):
            bucket = {}
            cache[kind] = bucket
        bucket[key] = items

    mutate_context(update)


def clear_cache(kind: str = "all") -> None:
    if kind not in {"all", "contests", "tasks", "submissions"}:
        raise ValueError(f"unknown cache bucket: {kind}")

    def clear(context: dict[str, Any]) -> None:
        cache = context.get("cache")
        if not isinstance(cache, dict):
            return
        if kind == "all":
            cache.clear()
        else:
            cache.pop(kind, None)

    mutate_context(clear)


def cached_items(kind: str, key: str) -> list[dict[str, Any]]:
    cache = load_context().get("cache", {})
    bucket = cache.get(kind, {}) if isinstance(cache, dict) else {}
    items = bucket.get(key, []) if isinstance(bucket, dict) else []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def prepare_history() -> str:
    paths = ensure_state_dir()
    if not os.path.exists(paths.history):
        atomic_write(paths.history, b"")
    else:
        os.chmod(paths.history, 0o600)
    return paths.history

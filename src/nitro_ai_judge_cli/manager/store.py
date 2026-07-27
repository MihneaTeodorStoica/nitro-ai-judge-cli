"""Transactional SQLite state for the Play manager."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterator


class ManagerStore:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        os.chmod(os.path.dirname(os.path.abspath(path)), 0o700)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.transaction() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS competitions (
                    key TEXT PRIMARY KEY,
                    organization TEXT NOT NULL,
                    competition TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    explicit_stopped INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY,
                    competition_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    options TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    error TEXT,
                    result TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS operation_competition_status
                    ON operations(competition_key, status);
                CREATE TABLE IF NOT EXISTS operation_events (
                    operation_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(operation_id, sequence),
                    FOREIGN KEY(operation_id) REFERENCES operations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legacy_adoptions (
                    competition_key TEXT PRIMARY KEY,
                    manifest TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    adopted_at REAL NOT NULL
                );
                """
            )
            interrupted = {
                "type": "operation_failed",
                "message": "The manager restarted before the operation completed",
                "stage": "interrupted",
                "logs": [],
            }
            db.execute(
                """
                UPDATE operations SET status='interrupted', stage='interrupted',
                    message=?, error=?, updated_at=?
                WHERE status IN ('queued', 'running')
                """,
                (interrupted["message"], json.dumps(interrupted), time.time()),
            )

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def upsert_competition(
        self, key: str, organization: str, competition: str, snapshot: dict[str, Any]
    ) -> None:
        with self.transaction() as db:
            existing = db.execute(
                "SELECT snapshot FROM competitions WHERE key=?", (key,)
            ).fetchone()
            snapshot = dict(snapshot)
            if existing:
                previous = json.loads(existing["snapshot"])
                for field in ("title", "featured", "competitionStart"):
                    if field not in snapshot and field in previous:
                        snapshot[field] = previous[field]
            db.execute(
                """
                INSERT INTO competitions(key, organization, competition, snapshot, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET snapshot=excluded.snapshot,
                    updated_at=excluded.updated_at
                """,
                (key, organization, competition, json.dumps(snapshot), time.time()),
            )

    def set_explicit_stopped(self, key: str, value: bool) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE competitions SET explicit_stopped=?, updated_at=? WHERE key=?",
                (1 if value else 0, time.time(), key),
            )

    def competitions(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM competitions ORDER BY organization, competition"
            ).fetchall()
        values = []
        for row in rows:
            value = json.loads(row["snapshot"])
            value["explicit_stopped"] = bool(row["explicit_stopped"])
            values.append(value)
        return values

    def competition(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM competitions WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["snapshot"])
        value["explicit_stopped"] = bool(row["explicit_stopped"])
        return value

    def active_operation(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT * FROM operations WHERE competition_key=?
                    AND status IN ('queued', 'running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (key,),
            ).fetchone()
        return self._operation_row(row) if row else None

    def latest_operation(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT * FROM operations WHERE competition_key=?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (key,),
            ).fetchone()
        return self._operation_row(row) if row else None

    def create_operation(
        self,
        operation_id: str,
        key: str,
        action: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        now = time.time()
        with self.transaction() as db:
            db.execute(
                """
                INSERT INTO operations
                    (id, competition_key, action, options, status, stage, message,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', 'queued', 'Operation queued', ?, ?)
                """,
                (operation_id, key, action, json.dumps(options, sort_keys=True), now, now),
            )
            db.execute(
                """
                INSERT INTO operation_events
                    (operation_id, sequence, stage, message, created_at)
                VALUES (?, 1, 'queued', 'Operation queued', ?)
                """,
                (operation_id, now),
            )
        return self.operation(operation_id) or {}

    def event(self, operation_id: str, stage: str, message: str) -> None:
        now = time.time()
        with self.transaction() as db:
            sequence = int(
                db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM operation_events WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()[0]
            )
            db.execute(
                """
                INSERT INTO operation_events(operation_id, sequence, stage, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (operation_id, sequence, stage, message, now),
            )
            db.execute(
                """
                UPDATE operations SET status='running', stage=?, message=?, updated_at=?
                WHERE id=?
                """,
                (stage, message, now, operation_id),
            )

    def finish(self, operation_id: str, result: dict[str, Any]) -> None:
        now = time.time()
        with self.transaction() as db:
            sequence = int(
                db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM operation_events WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()[0]
            )
            db.execute(
                """
                INSERT INTO operation_events(operation_id, sequence, stage, message, created_at)
                VALUES (?, ?, 'complete', 'Operation complete', ?)
                """,
                (operation_id, sequence, now),
            )
            db.execute(
                """
                UPDATE operations SET status='complete', stage='complete',
                    message='Operation complete', result=?, updated_at=? WHERE id=?
                """,
                (json.dumps(result), now, operation_id),
            )

    def fail(self, operation_id: str, error: dict[str, Any], *, status: str = "failed") -> None:
        stage = str(error.get("stage") or status)
        message = str(error.get("message") or "Operation failed")
        now = time.time()
        with self.transaction() as db:
            sequence = int(
                db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM operation_events WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()[0]
            )
            db.execute(
                """
                INSERT INTO operation_events(operation_id, sequence, stage, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (operation_id, sequence, stage, message, now),
            )
            db.execute(
                """
                UPDATE operations SET status=?, stage=?, message=?, error=?, updated_at=?
                WHERE id=?
                """,
                (status, stage, message, json.dumps(error), now, operation_id),
            )

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            events = self.connection.execute(
                """
                SELECT sequence, stage, message, created_at FROM operation_events
                WHERE operation_id=? ORDER BY sequence
                """,
                (operation_id,),
            ).fetchall()
        if row is None:
            return None
        value = self._operation_row(row)
        value["events"] = [dict(event) for event in events]
        return value

    @staticmethod
    def _operation_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "competition": row["competition_key"],
            "action": row["action"],
            "options": json.loads(row["options"]),
            "status": row["status"],
            "stage": row["stage"],
            "message": row["message"],
            "error": json.loads(row["error"]) if row["error"] else None,
            "result": json.loads(row["result"]) if row["result"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def put_credentials(self, value: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute(
                """
                INSERT INTO credentials(singleton, value, updated_at) VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (json.dumps(value), time.time()),
            )

    def credentials(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT value FROM credentials WHERE singleton=1"
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def delete_credentials(self) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM credentials WHERE singleton=1")

    def put_adoption(self, key: str, manifest: dict[str, Any], verified: bool) -> None:
        with self.transaction() as db:
            db.execute(
                """
                INSERT INTO legacy_adoptions(competition_key, manifest, verified, adopted_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(competition_key) DO UPDATE SET manifest=excluded.manifest,
                    verified=excluded.verified, adopted_at=excluded.adopted_at
                """,
                (key, json.dumps(manifest), int(verified), time.time()),
            )

    def adoption(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM legacy_adoptions WHERE competition_key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        return {
            "manifest": json.loads(row["manifest"]),
            "verified": bool(row["verified"]),
        }

    def delete_adoption(self, key: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM legacy_adoptions WHERE competition_key=?", (key,))

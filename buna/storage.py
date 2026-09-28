import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.lock = threading.RLock()
        self.db = self.root / "jobs.sqlite3"
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        os.chmod(self.db, 0o600)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db, timeout=15)

    def create(self, job: dict) -> dict:
        with self.lock, self.connect() as connection:
            connection.execute("INSERT INTO jobs VALUES (?, ?)", (job["id"], json.dumps(job)))
        return job

    def get(self, job_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return json.loads(row[0])

    def update(self, job_id: str, change: Callable[[dict], None]) -> dict:
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = json.loads(row[0])
            change(job)
            job["updated_at"] = now()
            connection.execute("UPDATE jobs SET data = ? WHERE id = ?", (json.dumps(job), job_id))
        return job

    def list(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT data FROM jobs").fetchall()
        return sorted((json.loads(row[0]) for row in rows), key=lambda job: job["created_at"], reverse=True)

    def folder(self, job_id: str) -> Path:
        # All callers validate UUIDs; also enforce confinement here.
        if not job_id or any(char not in "0123456789abcdef-" for char in job_id):
            raise ValueError("Invalid job identifier")
        folder = self.root / job_id
        folder.mkdir(mode=0o700, exist_ok=True)
        return folder


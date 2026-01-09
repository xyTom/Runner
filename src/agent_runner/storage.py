"""
SQLite storage for Agent Runner jobs.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List

from agent_runner.models import Job, JobStatus


class JobStorage:
    """
    SQLite-based persistent storage for Job objects.
    """

    def __init__(self, db_path: str = "jobs.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    upstream_repo TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    callback_url TEXT,
                    status TEXT NOT NULL,
                    fork_repo TEXT,
                    branch TEXT,
                    pr_url TEXT,
                    workflow_run_id INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def save_job(self, job: Job):
        data = job.to_dict()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO jobs (
                    job_id, upstream_repo, prompt, callback_url, status,
                    fork_repo, branch, pr_url, workflow_run_id, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["job_id"],
                    data["upstream_repo"],
                    data["prompt"],
                    data["callback_url"],
                    data["status"],
                    data["fork_repo"],
                    data["branch"],
                    data["pr_url"],
                    data["workflow_run_id"],
                    data["error"],
                    data["created_at"],
                    data["updated_at"],
                ),
            )
            conn.commit()

    def get_job(self, job_id: str) -> Optional[Job]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_job(row)
        return None

    def list_jobs(self, limit: int = 100) -> List[Job]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            return [self._row_to_job(row) for row in cursor.fetchall()]

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            upstream_repo=row["upstream_repo"],
            prompt=row["prompt"],
            callback_url=row["callback_url"],
            status=JobStatus(row["status"]),
            fork_repo=row["fork_repo"],
            branch=row["branch"],
            pr_url=row["pr_url"],
            workflow_run_id=row["workflow_run_id"],
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

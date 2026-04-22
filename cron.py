"""Cron service — periodic task execution using the background kiro-cli.

Storage: ~/.kirocli-gateway/crons.json
Execution: background kiro-cli (serialized via bg_lock)
Results: pushed to the creator's chat via send_callback
"""

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Type: (platform, chat_id, text) -> None
SendCallback = Callable[[str, str, str], None]


@dataclass
class CronJob:
    id: str = ""
    name: str = ""
    message: str = ""
    interval_secs: int = 3600
    last_run: float = 0.0
    paused: bool = False
    creator_platform: str = ""
    creator_chat_id: str = ""
    project: str = ""


class CronService:
    """Periodic task scheduler using the background kiro-cli."""

    def __init__(self, state_dir: str | Path, send_callback: SendCallback):
        self._path = Path(state_dir) / "crons.json"
        self._send = send_callback
        self._jobs: dict[str, CronJob] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for item in data:
                    job = CronJob(**{k: v for k, v in item.items() if k in CronJob.__dataclass_fields__})
                    if job.id:
                        self._jobs[job.id] = job
                log.info("[Cron] Loaded %d jobs", len(self._jobs))
            except Exception as e:
                log.warning("[Cron] Failed to load crons.json: %s", e)

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump([asdict(j) for j in self._jobs.values()], f, indent=2)
            os.replace(tmp_path, str(self._path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def start(self):
        """Start the scheduler thread."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("[Cron] Scheduler started (%d jobs)", len(self._jobs))

    def stop(self):
        self._stop.set()

    def add(self, name: str, message: str, interval_secs: int,
            platform: str, chat_id: str, project: str = "") -> CronJob:
        job = CronJob(
            id=uuid.uuid4().hex[:8],
            name=name,
            message=message,
            interval_secs=interval_secs,
            creator_platform=platform,
            creator_chat_id=chat_id,
            project=project,
        )
        self._jobs[job.id] = job
        self._save()
        log.info("[Cron] Added job %s: %s (every %ds)", job.id, name, interval_secs)
        return job

    def remove(self, job_id: str) -> bool:
        if job_id in self._jobs:
            del self._jobs[job_id]
            self._save()
            return True
        return False

    def pause(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job:
            job.paused = True
            self._save()
            return True
        return False

    def resume(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job:
            job.paused = False
            self._save()
            return True
        return False

    def list_jobs(self) -> list[CronJob]:
        return list(self._jobs.values())

    def get_due_jobs(self) -> list[CronJob]:
        """Return jobs that are due for execution."""
        now = time.time()
        due = []
        for job in self._jobs.values():
            if job.paused:
                continue
            if now - job.last_run >= job.interval_secs:
                due.append(job)
        return due

    def mark_executed(self, job_id: str):
        job = self._jobs.get(job_id)
        if job:
            job.last_run = time.time()
            self._save()

    # execute_callback is set by gateway after _bg is ready
    execute_callback: Callable[[CronJob], str] | None = None

    def _loop(self):
        while not self._stop.wait(timeout=30):
            due = self.get_due_jobs()
            for job in due:
                self.mark_executed(job.id)
                if self.execute_callback:
                    try:
                        result_text = self.execute_callback(job)
                        self._send(job.creator_platform, job.creator_chat_id,
                                   f"⏰ **[Cron: {job.name}]**\n\n{result_text}")
                    except Exception as e:
                        log.warning("[Cron] Job %s failed: %s", job.id, e)
                        self._send(job.creator_platform, job.creator_chat_id,
                                   f"⏰ **[Cron: {job.name}]** ❌ Failed: {e}")

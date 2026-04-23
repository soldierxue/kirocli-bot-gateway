"""Cron service — periodic task execution using the background kiro-cli.

Features:
- Interval-based scheduling (--every N seconds)
- 5-field cron expressions (--schedule "0 9 * * 1-5")
- Markdown job files (~/.kirocli-gateway/jobs/*.md)
- Heartbeat: periodic check-in, silent when nothing to report
- Quiet hours for heartbeat

Storage: ~/.kirocli-gateway/crons.json + jobs/*.md
"""

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SendCallback = Callable[[str, str, str], None]

_DEFAULT_HEARTBEAT_PROMPT = (
    "Check on things briefly. If there's something worth mentioning "
    "(file changes, pending tasks, errors in recent work), say it naturally "
    "and short — like texting a friend. If nothing needs attention, respond "
    "with exactly: HEARTBEAT_OK"
)


# ── Cron expression matching ──

def _cron_matches(expression: str, dt: datetime) -> bool:
    """Check if a 5-field cron expression matches the given datetime.

    Fields: minute hour day-of-month month day-of-week
    Supports: *, specific values, ranges (1-5), lists (1,3,5), */N steps.
    """
    fields = expression.strip().split()
    if len(fields) != 5:
        return False
    checks = [
        (fields[0], dt.minute, 0, 59),
        (fields[1], dt.hour, 0, 23),
        (fields[2], dt.day, 1, 31),
        (fields[3], dt.month, 1, 12),
        (fields[4], dt.isoweekday() % 7, 0, 6),  # 0=Sun
    ]
    return all(_field_matches(f, v, lo, hi) for f, v, lo, hi in checks)


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            step_int = int(step)
            start = lo if base == "*" else int(base)
            if step_int > 0 and (value - start) % step_int == 0 and value >= start:
                return True
        elif "-" in part:
            a, b = part.split("-", 1)
            if int(a) <= value <= int(b):
                return True
        else:
            if value == int(part):
                return True
    return False


def cron_to_human(expr: str) -> str:
    """Convert 5-field cron expression to human-readable text."""
    fields = expr.strip().split()
    if len(fields) != 5:
        return expr
    minute, hour, dom, month, dow = fields

    if expr.strip() == "* * * * *":
        return "每分钟"
    if minute.startswith("*/"):
        return f"每{minute[2:]}分钟"
    if hour.startswith("*/"):
        return f"每{hour[2:]}小时"

    if dom == "*" and month == "*":
        dow_map = {
            "1-5": "工作日", "0,6": "周末", "*": "每天",
            "1": "周一", "2": "周二", "3": "周三",
            "4": "周四", "5": "周五", "6": "周六", "0": "周日", "7": "周日",
        }
        day_str = dow_map.get(dow, f"周{dow}")
        return f"{day_str} {hour.zfill(2)}:{minute.zfill(2)}"

    return expr


# ── Job file parsing ──

def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse simple YAML-like frontmatter from Markdown."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    meta: dict = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2]


# ── Data ──

@dataclass
class CronJob:
    id: str = ""
    name: str = ""
    message: str = ""
    interval_secs: int = 0
    schedule: str = ""           # 5-field cron expression
    last_run: float = 0.0
    paused: bool = False
    creator_platform: str = ""
    creator_chat_id: str = ""
    project: str = ""


class CronService:
    """Periodic task scheduler with heartbeat support."""

    def __init__(self, state_dir: str | Path, send_callback: SendCallback,
                 heartbeat_enabled: bool = False, heartbeat_interval: int = 900,
                 heartbeat_target: str = "", heartbeat_exclude: str = ""):
        self._state_dir = Path(state_dir)
        self._path = self._state_dir / "crons.json"
        self._jobs_dir = self._state_dir / "jobs"
        self._send = send_callback
        self._jobs: dict[str, CronJob] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Heartbeat
        self._hb_enabled = heartbeat_enabled
        self._hb_interval = heartbeat_interval
        self._hb_target = heartbeat_target
        self._hb_exclude = heartbeat_exclude
        self._last_heartbeat: float = 0.0
        self._last_job_scan: float = 0.0
        # Callback to check if background kiro-cli is busy (set by gateway)
        self._bg_busy_check: Callable[[], bool] | None = None
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for item in data:
                    job = CronJob(**{k: v for k, v in item.items()
                                    if k in CronJob.__dataclass_fields__})
                    if job.id:
                        self._jobs[job.id] = job
                log.info("[Cron] Loaded %d jobs", len(self._jobs))
            except Exception as e:
                log.warning("[Cron] Failed to load crons.json: %s", e)

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Only save non-file jobs (file: prefix jobs come from disk)
        saveable = [asdict(j) for j in self._jobs.values()
                    if not j.id.startswith("file:")]
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(saveable, f, indent=2)
            os.replace(tmp_path, str(self._path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _scan_job_files(self):
        """Scan jobs/ directory for Markdown job definitions."""
        if not self._jobs_dir.exists():
            return
        # Remove old file-based jobs
        old_file_keys = [k for k in self._jobs if k.startswith("file:")]
        for k in old_file_keys:
            del self._jobs[k]

        for f in self._jobs_dir.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8")
                meta, prompt = _parse_frontmatter(content)
                if not prompt.strip():
                    continue
                job_id = f"file:{f.stem}"
                self._jobs[job_id] = CronJob(
                    id=job_id,
                    name=meta.get("name", f.stem),
                    message=prompt.strip(),
                    schedule=meta.get("schedule", ""),
                    interval_secs=int(meta.get("every", 0)),
                    creator_platform=meta.get("platform", ""),
                    creator_chat_id=meta.get("chat_id", ""),
                )
            except Exception as e:
                log.warning("[Cron] Failed to parse job file %s: %s", f.name, e)

    def start(self):
        self._scan_job_files()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("[Cron] Scheduler started (%d jobs, heartbeat=%s)",
                 len(self._jobs), self._hb_enabled)

    def stop(self):
        self._stop.set()

    def add(self, name: str, message: str, interval_secs: int = 0,
            platform: str = "", chat_id: str = "", project: str = "",
            schedule: str = "") -> CronJob:
        job = CronJob(
            id=uuid.uuid4().hex[:8], name=name, message=message,
            interval_secs=interval_secs, schedule=schedule,
            creator_platform=platform, creator_chat_id=chat_id, project=project,
        )
        self._jobs[job.id] = job
        self._save()
        sched_info = f"schedule={schedule}" if schedule else f"every {interval_secs}s"
        log.info("[Cron] Added job %s: %s (%s)", job.id, name, sched_info)
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
        now = time.time()
        now_dt = datetime.now()
        due = []
        for job in self._jobs.values():
            if job.paused:
                continue
            if job.schedule:
                if now - job.last_run >= 60 and _cron_matches(job.schedule, now_dt):
                    due.append(job)
            elif job.interval_secs > 0:
                if now - job.last_run >= job.interval_secs:
                    due.append(job)
        return due

    def mark_executed(self, job_id: str):
        job = self._jobs.get(job_id)
        if job:
            job.last_run = time.time()
            if not job.id.startswith("file:"):
                self._save()

    execute_callback: Callable[[CronJob], str] | None = None

    def _in_exclude_window(self) -> bool:
        """Check if current time falls in heartbeat quiet hours."""
        if not self._hb_exclude:
            return False
        now = datetime.now()
        weekday = now.isoweekday()  # 1=Mon, 7=Sun
        current = now.strftime("%H:%M")
        # Parse "23:00-07:00/1-5" format
        parts = self._hb_exclude.split("/")
        time_range = parts[0] if parts else ""
        day_range = parts[1] if len(parts) > 1 else ""
        if day_range:
            try:
                a, b = day_range.split("-")
                if not (int(a) <= weekday <= int(b)):
                    return False
            except (ValueError, IndexError):
                pass
        if "-" in time_range:
            start, end = time_range.split("-", 1)
            if start <= end:
                return start <= current <= end
            else:
                return current >= start or current <= end
        return False

    def _check_heartbeat(self):
        if not self._hb_enabled or not self.execute_callback:
            return
        now = time.time()
        if now - self._last_heartbeat < self._hb_interval:
            return
        if self._in_exclude_window():
            return
        # Skip if background kiro-cli is busy (consolidation or cron running)
        if self._bg_busy_check and self._bg_busy_check():
            log.debug("[Cron] Heartbeat skipped — background busy")
            return
        self._last_heartbeat = now
        try:
            result = self.execute_callback(CronJob(
                id="_heartbeat", name="Heartbeat", message=_DEFAULT_HEARTBEAT_PROMPT))
            if "HEARTBEAT_OK" not in result:
                target = self._hb_target
                if target and ":" in target:
                    p, c = target.split(":", 1)
                    self._send(p, c, f"💓 {result}")
                else:
                    log.info("[Cron] Heartbeat: %s (no target configured)", result[:80])
        except Exception as e:
            log.warning("[Cron] Heartbeat failed: %s", e)

    def _loop(self):
        while not self._stop.wait(timeout=30):
            # Rescan job files every 60s
            now = time.time()
            if now - self._last_job_scan >= 60:
                self._last_job_scan = now
                self._scan_job_files()

            # Heartbeat
            self._check_heartbeat()

            # Cron jobs
            due = self.get_due_jobs()
            for job in due:
                self.mark_executed(job.id)
                if self.execute_callback:
                    try:
                        result_text = self.execute_callback(job)
                        if job.creator_platform and job.creator_chat_id:
                            self._send(job.creator_platform, job.creator_chat_id,
                                       f"⏰ **[Cron: {job.name}]**\n\n{result_text}")
                    except Exception as e:
                        log.warning("[Cron] Job %s failed: %s", job.id, e)
                        if job.creator_platform and job.creator_chat_id:
                            self._send(job.creator_platform, job.creator_chat_id,
                                       f"⏰ **[Cron: {job.name}]** ❌ Failed: {e}")

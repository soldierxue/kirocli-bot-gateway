"""Task runner — multi-step task decomposition and execution.

Uses the background kiro-cli to decompose tasks into steps with dependency graphs,
then executes steps in parallel groups using temporary kiro-cli instances.

Supports: LLM decomposition, topological sort, parallel execution (max 2),
failure retry (3x), acceptance check.
"""

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from acp_client import ACPClient

log = logging.getLogger(__name__)

_TASK_MAX_PARALLEL = 2
_STEP_TIMEOUT = 600      # 10 min per step
_TASK_TIMEOUT = 1800     # 30 min total
_MAX_RETRIES = 3

# Type: (platform, chat_id, text) -> None
SendCallback = Callable[[str, str, str], None]


@dataclass
class TaskStep:
    step: int = 0
    description: str = ""
    prompt: str = ""
    depends_on: list[int] = field(default_factory=list)
    status: str = "pending"  # pending, running, ok, failed
    result: str = ""
    error: str = ""


@dataclass
class Task:
    id: str = ""
    description: str = ""
    steps: list[TaskStep] = field(default_factory=list)
    status: str = "planning"  # planning, waiting, running, done, failed, cancelled
    platform: str = ""
    chat_id: str = ""
    project: str = ""
    started_at: float = 0.0


def group_parallel_steps(steps: list[TaskStep]) -> list[list[TaskStep]]:
    """Topological sort into parallel execution groups."""
    completed: set[int] = set()
    remaining = list(steps)
    groups: list[list[TaskStep]] = []

    while remaining:
        ready = [s for s in remaining
                 if all(d in completed for d in s.depends_on)]
        if not ready:
            # Circular dependency or missing step — run rest serially
            groups.append(remaining)
            break
        groups.append(ready)
        for s in ready:
            completed.add(s.step)
            remaining.remove(s)

    return groups


class TaskRunner:
    """Decomposes and executes multi-step tasks."""

    def __init__(self, cli_path: str, default_cwd: str, send_callback: SendCallback):
        self._cli_path = cli_path
        self._default_cwd = default_cwd
        self._send = send_callback
        self._tasks: dict[str, Task] = {}
        self._active_task: Task | None = None
        self._cancel_event = threading.Event()

    @property
    def active_task(self) -> Task | None:
        return self._active_task

    def decompose(self, bg_acp: ACPClient, bg_session_id: str,
                  description: str) -> list[TaskStep]:
        """Use background kiro-cli to decompose a task into steps."""
        prompt = (
            "Decompose this task into 2-8 concrete steps. Return ONLY a JSON array:\n"
            '[{"step": 1, "description": "...", "prompt": "detailed instruction for this step", '
            '"depends_on": []}]\n\n'
            "Rules:\n"
            "- Each step.prompt must be a self-contained instruction an AI agent can execute\n"
            "- Use depends_on to declare which steps must complete first (by step number)\n"
            "- Steps with no dependencies (or only completed dependencies) can run in parallel\n"
            "- The last step should verify/test the overall result\n\n"
            f"Task: {description}\n\n"
            "Return ONLY the JSON array, no markdown fences."
        )
        result = bg_acp.session_prompt(bg_session_id, prompt, timeout=120)
        return self._parse_steps(result.text)

    def _parse_steps(self, raw: str) -> list[TaskStep]:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("[TaskRunner] Failed to parse steps JSON")
            return []

        if not isinstance(data, list):
            return []

        steps = []
        for item in data:
            if isinstance(item, dict):
                steps.append(TaskStep(
                    step=item.get("step", len(steps) + 1),
                    description=item.get("description", ""),
                    prompt=item.get("prompt", item.get("description", "")),
                    depends_on=item.get("depends_on", []),
                ))
        return steps

    def format_plan(self, task: Task) -> str:
        """Format task plan for display to user."""
        groups = group_parallel_steps(task.steps)
        lines = [f"📋 **Task plan** ({len(task.steps)} steps):\n"]
        for gi, group in enumerate(groups, 1):
            if len(group) == 1:
                s = group[0]
                lines.append(f"  Group {gi}: [{s.step}] {s.description}")
            else:
                parts = " ║ ".join(f"[{s.step}] {s.description}" for s in group)
                lines.append(f"  Group {gi}: {parts}")
        lines.append("\nReply **go** to start, **cancel** to abort")
        return "\n".join(lines)

    def run(self, task: Task, bg_acp: ACPClient, bg_session_id: str):
        """Execute all steps of a task. Called in a background thread."""
        self._active_task = task
        self._cancel_event.clear()
        task.status = "running"
        task.started_at = time.time()

        groups = group_parallel_steps(task.steps)
        all_ok = True

        try:
            for gi, group in enumerate(groups, 1):
                if self._cancel_event.is_set():
                    task.status = "cancelled"
                    self._send(task.platform, task.chat_id, "⏹️ Task cancelled")
                    return

                # Check total timeout
                if time.time() - task.started_at > _TASK_TIMEOUT:
                    task.status = "failed"
                    self._send(task.platform, task.chat_id,
                               f"⏱️ Task timed out after {_TASK_TIMEOUT // 60} minutes")
                    return

                self._send(task.platform, task.chat_id,
                           f"▶️ Running group {gi}/{len(groups)}...")
                self._execute_group(group, task)

                # Check for failures
                failed = [s for s in group if s.status == "failed"]
                if failed:
                    for s in failed:
                        self._send(task.platform, task.chat_id,
                                   f"❌ Step {s.step} failed: {s.error}")
                    all_ok = False
                    break

                for s in group:
                    self._send(task.platform, task.chat_id,
                               f"✅ Step {s.step}: {s.description}")

            # Summary
            elapsed = time.time() - task.started_at
            ok_count = sum(1 for s in task.steps if s.status == "ok")
            task.status = "done" if all_ok else "failed"
            status = "✅" if all_ok else "⚠️"
            self._send(task.platform, task.chat_id,
                       f"📋 **Task {'complete' if all_ok else 'incomplete'}** {status}\n"
                       f"  {ok_count}/{len(task.steps)} steps succeeded\n"
                       f"  Duration: {elapsed:.0f}s")

        except Exception as e:
            task.status = "failed"
            log.exception("[TaskRunner] Task %s failed: %s", task.id, e)
            self._send(task.platform, task.chat_id, f"❌ Task failed: {e}")
        finally:
            self._active_task = None

    def cancel(self):
        self._cancel_event.set()

    def _execute_group(self, group: list[TaskStep], task: Task):
        """Execute a group of independent steps, up to _TASK_MAX_PARALLEL at a time."""
        if len(group) == 1:
            self._execute_step(group[0], task)
            return

        sem = threading.Semaphore(_TASK_MAX_PARALLEL)
        threads = []

        for step in group:
            sem.acquire()
            t = threading.Thread(
                target=self._execute_step_with_sem,
                args=(step, task, sem),
                daemon=True,
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=_STEP_TIMEOUT)

    def _execute_step_with_sem(self, step: TaskStep, task: Task, sem: threading.Semaphore):
        try:
            self._execute_step(step, task)
        finally:
            sem.release()

    def _execute_step(self, step: TaskStep, task: Task):
        """Execute a single step using a temporary kiro-cli instance."""
        step.status = "running"
        cwd = task.project or self._default_cwd

        acp = ACPClient(cli_path=self._cli_path)
        try:
            acp.start(cwd=cwd)
            session_id, _ = acp.session_new(cwd)

            for attempt in range(_MAX_RETRIES):
                if self._cancel_event.is_set():
                    step.status = "failed"
                    step.error = "cancelled"
                    return
                try:
                    result = acp.session_prompt(session_id, step.prompt,
                                                timeout=_STEP_TIMEOUT)
                    step.status = "ok"
                    step.result = result.text[:500] if result.text else ""
                    return
                except Exception as e:
                    if attempt < _MAX_RETRIES - 1:
                        log.warning("[TaskRunner] Step %d attempt %d failed: %s",
                                    step.step, attempt + 1, e)
                        time.sleep(2)
                    else:
                        step.status = "failed"
                        step.error = str(e)
        except Exception as e:
            step.status = "failed"
            step.error = f"kiro-cli start failed: {e}"
        finally:
            try:
                acp.stop()
            except Exception:
                pass

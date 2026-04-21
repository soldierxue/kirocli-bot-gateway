"""Memory consolidator — extracts structured knowledge from conversations using LLM.

Reads recent conversation history from kiro-cli's session JSONL file,
sends a consolidation prompt to kiro-cli, and writes extracted preferences,
projects, and lessons to the MemoryStore.

Triggered after 15+ messages when the chat has been idle for 60+ seconds.
"""

import json
import logging
from pathlib import Path

from memory import MemoryStore
from session_map import KIRO_SESSIONS_DIR

log = logging.getLogger(__name__)

# Consolidation thresholds
_MIN_MESSAGES = 15       # Minimum messages before consolidation is considered
_MAX_HISTORY_CHARS = 8000  # Max chars of conversation to send for consolidation


class MemoryConsolidator:
    """Extract structured memory from conversations using the chat's own kiro-cli."""

    def __init__(self, memory: MemoryStore):
        self._memory = memory
        # effective_key → message count since last consolidation
        self._msg_counts: dict[str, int] = {}
        # Keys that have accumulated enough messages and are waiting for idle
        self._pending: set[str] = set()
        # Keys currently being consolidated (prevent double-run)
        self._running: set[str] = set()

    def on_message(self, effective_key: str):
        """Track message count. Called after each successful prompt."""
        self._msg_counts[effective_key] = self._msg_counts.get(effective_key, 0) + 1
        if self._msg_counts.get(effective_key, 0) >= _MIN_MESSAGES:
            self._pending.add(effective_key)

    def should_consolidate(self, effective_key: str) -> bool:
        """Check if this key is pending consolidation and not already running."""
        return effective_key in self._pending and effective_key not in self._running

    def mark_running(self, effective_key: str):
        """Mark consolidation as in-progress."""
        self._running.add(effective_key)
        self._pending.discard(effective_key)

    def mark_done(self, effective_key: str):
        """Mark consolidation as complete. Reset message counter."""
        self._running.discard(effective_key)
        self._msg_counts[effective_key] = 0

    def read_recent_conversation(self, session_id: str) -> str:
        """Read recent conversation from kiro-cli's session JSONL file.

        Returns a formatted string of recent user/assistant exchanges.
        """
        jsonl_path = KIRO_SESSIONS_DIR / f"{session_id}.jsonl"
        if not jsonl_path.exists():
            return ""

        lines: list[str] = []
        total_chars = 0

        try:
            raw_lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
            # Read from the end (most recent first)
            for raw in reversed(raw_lines):
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                role = entry.get("role", "")
                content = ""
                # Extract text content from various formats
                if isinstance(entry.get("content"), str):
                    content = entry["content"]
                elif isinstance(entry.get("content"), list):
                    parts = []
                    for block in entry["content"]:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                    content = "\n".join(parts)
                elif isinstance(entry.get("message"), str):
                    content = entry["message"]

                if not content or not role:
                    continue

                # Truncate very long messages
                if len(content) > 500:
                    content = content[:500] + "...[truncated]"

                line = f"{role.upper()}: {content}"
                if total_chars + len(line) > _MAX_HISTORY_CHARS:
                    break
                lines.append(line)
                total_chars += len(line)
        except Exception as e:
            log.warning("[Consolidator] Failed to read JSONL %s: %s", jsonl_path, e)
            return ""

        lines.reverse()  # Chronological order
        return "\n\n".join(lines)

    def build_prompt(self, conversation: str, workspace_id: str = "_global") -> str:
        """Build the consolidation prompt for kiro-cli."""
        current_prefs = self._memory.read_preferences()
        current_projects = self._memory.read_projects(workspace_id)
        current_lessons = self._memory.read_lessons()

        return f"""You are a memory extraction agent. Analyze the conversation below
and return a JSON object with these keys:

1. "preferences_update": Updated user preferences (complete file content).
   Merge with existing, remove contradictions, keep newest.
   Keep "# User Preferences" header. Return existing content if nothing changed.
   NOTE: Preferences are GLOBAL — they apply across all projects.

2. "projects_update": Updated active projects (complete file content).
   Only active projects, remove stale entries, update facts.
   Keep "# Active Projects" header. Return existing content if nothing changed.
   NOTE: Projects are WORKSPACE-SCOPED — only for the current project.

3. "lessons": The COMPLETE updated list of corrections and rules.
   Merge with existing lessons, remove duplicates and contradictions.
   Each: {{"rule": "...", "category": "preference|tool|knowledge"}}.
   Return the full deduplicated list (not just new ones).
   Empty [] ONLY if there are truly zero lessons (existing + new).
   NOTE: Lessons are GLOBAL — they apply across all projects.

4. "history_entry": A concise paragraph (2-5 sentences) summarizing what
   happened in this conversation. Include key decisions, outcomes, and facts.
   Use present tense. Example: "User fixed login bug in myapp using
   pytest-asyncio strict mode. Decided to add retry logic for S3 uploads."
   Return empty string "" if the conversation was trivial (greetings only).

## Current Preferences (global)
{current_prefs or '(empty)'}

## Current Projects (this workspace)
{current_projects or '(empty)'}

## Current Lessons (global)
{current_lessons or '(empty)'}

## Conversation to Analyze
{conversation}

Respond with ONLY valid JSON, no markdown fences, no explanation."""

    def apply_result(self, raw_text: str, workspace_id: str = "_global") -> bool:
        """Parse LLM response and write to memory. Returns True if anything changed."""
        # Strip markdown fences if present
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            log.warning("[Consolidator] Failed to parse LLM response as JSON")
            return False

        changed = False

        if prefs := result.get("preferences_update"):
            if isinstance(prefs, str) and prefs.strip():
                self._memory.write_preferences(prefs)
                changed = True
                log.info("[Consolidator] Updated preferences")

        if projects := result.get("projects_update"):
            if isinstance(projects, str) and projects.strip():
                self._memory.write_projects(projects, workspace_id)
                changed = True
                log.info("[Consolidator] Updated projects (ws=%s)", workspace_id)

        if lessons := result.get("lessons"):
            if isinstance(lessons, list) and lessons:
                # Full replacement: LLM returns the complete deduplicated list
                header = "# Learned Corrections\n\n<!-- Rules from user feedback -->\n"
                lines = []
                for lesson in lessons:
                    rule = lesson.get("rule", "") if isinstance(lesson, dict) else str(lesson)
                    if rule:
                        lines.append(f"- {rule}")
                if lines:
                    content = header + "\n".join(lines) + "\n"
                    self._memory._safe_write(self._memory._lessons, content)
                    changed = True
                    log.info("[Consolidator] Replaced lessons (%d rules)", len(lines))

        if history_entry := result.get("history_entry"):
            if isinstance(history_entry, str) and history_entry.strip():
                self._memory.append_history(history_entry)
                changed = True
                log.info("[Consolidator] Appended history entry")

        return changed

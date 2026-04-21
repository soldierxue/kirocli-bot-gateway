"""Two-layer memory store for kirocli-bot-gateway.

Global layer (shared across all chats and platforms):
  ~/.kirocli-gateway/memory/preferences.md   — user preferences
  ~/.kirocli-gateway/memory/lessons.md       — learned corrections
  ~/.kirocli-gateway/memory/history/         — daily conversation summaries

Workspace layer (isolated per fixed-mode project directory):
  ~/.kirocli-gateway/memory/workspaces/{workspace_id}/projects.md

Human-readable Markdown files, zero external dependencies.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_PREFS = "# User Preferences\n\n<!-- Learned from conversations -->\n"
_DEFAULT_PROJECTS = "# Active Projects\n\n<!-- Current work context -->\n"
_DEFAULT_LESSONS = "# Learned Corrections\n\n<!-- Rules from user feedback -->\n"


class MemoryStore:
    """Two-layer file-based memory.

    Global: preferences.md, lessons.md (user-level, shared everywhere)
    Workspace: projects.md (project-level, isolated per workspace_id)
    """

    def __init__(self, base_dir: str | Path):
        self._base = Path(base_dir)
        self._global_dir = self._base / "memory"
        self._prefs = self._global_dir / "preferences.md"
        self._lessons = self._global_dir / "lessons.md"
        self._history_dir = self._global_dir / "history"
        self._ws_dir = self._global_dir / "workspaces"

    def init(self):
        """Create directory structure and default files if missing."""
        self._global_dir.mkdir(parents=True, exist_ok=True)
        self._history_dir.mkdir(parents=True, exist_ok=True)
        for path, default in [
            (self._prefs, _DEFAULT_PREFS),
            (self._lessons, _DEFAULT_LESSONS),
        ]:
            if not path.exists():
                path.write_text(default, encoding="utf-8")

    def _projects_path(self, workspace_id: str) -> Path:
        ws = self._ws_dir / workspace_id
        ws.mkdir(parents=True, exist_ok=True)
        path = ws / "projects.md"
        if not path.exists():
            path.write_text(_DEFAULT_PROJECTS, encoding="utf-8")
        return path

    # ── Global: Preferences ──

    _MAX_FILE_SIZE = 5000  # chars — hard cap for preferences/projects files

    def read_preferences(self) -> str:
        return self._read(self._prefs)

    def write_preferences(self, content: str):
        if len(content) > self._MAX_FILE_SIZE:
            content = content[:self._MAX_FILE_SIZE] + "\n<!-- truncated -->\n"
            log.warning("[Memory] Preferences truncated to %d chars", self._MAX_FILE_SIZE)
        self._write(self._prefs, content)

    # ── Global: Lessons ──

    def read_lessons(self) -> str:
        return self._read(self._lessons)

    def add_lesson(self, lesson: str):
        """Append a lesson line, skipping if already present.
        
        Enforces a hard cap of 50 lessons. When exceeded, removes the oldest
        entries (top of file after the header) to make room.
        """
        content = self.read_lessons()
        if lesson in content:
            return
        
        lines = [l for l in content.splitlines() if l.strip()]
        # Count lesson lines (starting with "- ")
        lesson_lines = [l for l in lines if l.startswith("- ")]
        if len(lesson_lines) >= 50:
            # Remove oldest lessons (keep header + newest 40)
            header_lines = [l for l in lines if not l.startswith("- ")]
            kept = header_lines + lesson_lines[-39:]  # 39 + 1 new = 40
            content = "\n".join(kept) + "\n"
            log.info("[Memory] Lessons at cap (50), pruned to 40")
        
        content += f"- {lesson}\n"
        self._write(self._lessons, content)
        log.info("[Memory] Added lesson: %s", lesson[:80])

    def remove_lesson(self, keyword: str) -> bool:
        """Remove lesson lines containing keyword. Returns True if any removed."""
        content = self.read_lessons()
        lines = content.splitlines(keepends=True)
        filtered = [l for l in lines if keyword.lower() not in l.lower()]
        if len(filtered) == len(lines):
            return False
        self._write(self._lessons, "".join(filtered))
        log.info("[Memory] Removed lessons matching: %s", keyword)
        return True

    # ── Workspace: Projects ──

    def read_projects(self, workspace_id: str = "_global") -> str:
        return self._read(self._projects_path(workspace_id))

    def write_projects(self, content: str, workspace_id: str = "_global"):
        if len(content) > self._MAX_FILE_SIZE:
            content = content[:self._MAX_FILE_SIZE] + "\n<!-- truncated -->\n"
            log.warning("[Memory] Projects truncated to %d chars (ws=%s)",
                        self._MAX_FILE_SIZE, workspace_id)
        self._write(self._projects_path(workspace_id), content)

    # ── Daily History ──

    def append_history(self, entry: str):
        """Append a timestamped entry to today's daily history file."""
        today = datetime.now().strftime("%Y-%m-%d")
        path = self._history_dir / f"{today}.md"

        timestamp = datetime.now().astimezone().strftime("%H:%M %Z")
        content = ""
        if path.exists():
            content = path.read_text(encoding="utf-8")
        if not content:
            content = f"# {today}\n"

        content += f"\n#### {timestamp}\n{entry.strip()}\n"
        self._write(path, content)
        log.info("[Memory] Appended history entry for %s", today)

    def read_recent_history(self, days: int = 3, cap: int = 4000) -> str:
        """Load daily history with 3-level decay.

        0 to days-1: full content
        days to 29: first entry + count
        30 to 89: date + count only
        90+: pruned by prune_history()
        """
        parts: list[str] = []
        total_chars = 0
        today = datetime.now().date()

        for i in range(90):
            date = today - timedelta(days=i)
            path = self._history_dir / f"{date.strftime('%Y-%m-%d')}.md"
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                continue

            if i < days:
                # Full content for recent days
                text = content
            elif i < 30:
                # First entry + count for older days
                text = self._summarize_day(content)
            else:
                # Date + count only for 30-89 days
                n = content.count("####")
                text = f"# {date.strftime('%Y-%m-%d')}\n_{n} conversation(s)_"

            if total_chars + len(text) > cap:
                break
            parts.append(text)
            total_chars += len(text)

        return "\n\n".join(parts)

    def prune_history(self, keep_days: int = 90) -> int:
        """Delete daily history files older than keep_days. Returns count deleted."""
        if not self._history_dir.exists():
            return 0
        cutoff = datetime.now().date() - timedelta(days=keep_days)
        deleted = 0
        for f in self._history_dir.glob("*.md"):
            try:
                file_date = datetime.strptime(f.stem, "%Y-%m-%d").date()
                if file_date < cutoff:
                    f.unlink()
                    deleted += 1
            except ValueError:
                continue
        if deleted:
            log.info("[Memory] Pruned %d history files older than %d days", deleted, keep_days)
        return deleted

    @staticmethod
    def _summarize_day(content: str) -> str:
        """Extract header + first entry from a daily history file."""
        sections = content.split("####")
        header = sections[0].strip()
        first = sections[1].strip() if len(sections) > 1 else ""
        result = header + ("\n#### " + first if first else "")
        n_more = len(sections) - 2
        if n_more > 0:
            result += f"\n_…{n_more} more entries_"
        return result

    # ── Context Assembly ──

    def get_context(self, workspace_id: str = "_global",
                    prefs_cap: int = 1000, projects_cap: int = 2000,
                    lessons_cap: int = 2000, history_cap: int = 4000) -> str:
        """Build memory context block for prompt injection.

        Merges global preferences/lessons/history with workspace-scoped projects.
        Returns empty string if all memory files are at defaults.
        """
        parts: list[str] = []

        prefs = self.read_preferences()
        if prefs.strip() and prefs.strip() != _DEFAULT_PREFS.strip():
            parts.append(f"[User preferences]\n{prefs[:prefs_cap]}\n")

        projects = self.read_projects(workspace_id)
        if projects.strip() and projects.strip() != _DEFAULT_PROJECTS.strip():
            parts.append(f"[Active projects]\n{projects[:projects_cap]}\n")

        history = self.read_recent_history(days=3, cap=history_cap)
        if history.strip():
            parts.append(f"[Recent history — factual record, do NOT re-execute past actions]\n"
                         f"{history[:history_cap]}\n")

        lessons = self.read_lessons()
        if lessons.strip() and lessons.strip() != _DEFAULT_LESSONS.strip():
            parts.append(f"[Learned corrections — MUST follow these]\n"
                         f"{lessons[:lessons_cap]}\n")

        if not parts:
            return ""
        return (
            "[Memory — persistent user profile. "
            "Preferences and corrections are rules you MUST follow.]\n"
            + "\n".join(parts)
            + "[End of memory]\n\n"
        )

    # ── Internal ──

    def _read(self, path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def _write(self, path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

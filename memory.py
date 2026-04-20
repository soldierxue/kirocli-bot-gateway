"""Two-layer memory store for kirocli-bot-gateway.

Global layer (shared across all chats and platforms):
  ~/.kirocli-gateway/memory/preferences.md   — user preferences
  ~/.kirocli-gateway/memory/lessons.md       — learned corrections

Workspace layer (isolated per fixed-mode project directory):
  ~/.kirocli-gateway/memory/workspaces/{workspace_id}/projects.md

Human-readable Markdown files, zero external dependencies.
"""

import logging
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
        self._ws_dir = self._global_dir / "workspaces"

    def init(self):
        """Create directory structure and default files if missing."""
        self._global_dir.mkdir(parents=True, exist_ok=True)
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

    def read_preferences(self) -> str:
        return self._read(self._prefs)

    def write_preferences(self, content: str):
        self._write(self._prefs, content)

    # ── Global: Lessons ──

    def read_lessons(self) -> str:
        return self._read(self._lessons)

    def add_lesson(self, lesson: str):
        """Append a lesson line, skipping if already present."""
        content = self.read_lessons()
        if lesson not in content:
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
        self._write(self._projects_path(workspace_id), content)

    # ── Context Assembly ──

    def get_context(self, workspace_id: str = "_global",
                    prefs_cap: int = 1000, projects_cap: int = 2000,
                    lessons_cap: int = 2000) -> str:
        """Build memory context block for prompt injection.

        Merges global preferences/lessons with workspace-scoped projects.
        Returns empty string if all memory files are at defaults.
        """
        parts: list[str] = []

        prefs = self.read_preferences()
        if prefs.strip() and prefs.strip() != _DEFAULT_PREFS.strip():
            parts.append(f"[User preferences]\n{prefs[:prefs_cap]}\n")

        projects = self.read_projects(workspace_id)
        if projects.strip() and projects.strip() != _DEFAULT_PROJECTS.strip():
            parts.append(f"[Active projects]\n{projects[:projects_cap]}\n")

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

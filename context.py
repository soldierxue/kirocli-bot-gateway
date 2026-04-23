"""Context builder — injects memory into the first message of a new session.

On new sessions (is_new_session=True): prepends date/time + memory + style guide.
On follow-up messages: returns text as-is, trusting ACP native history.
This matches MeshClaw's "context prefix injection" pattern.
"""

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from config import Config
from memory import MemoryStore, _DEFAULT_PREFS

log = logging.getLogger(__name__)

_COMMUNICATION_STYLE = """[Communication style]
- Be concise. Skip pleasantries like "Great question!" — just help.
- In chat, keep responses short and natural. Split long answers into paragraphs.
- Try to solve problems yourself before asking clarifying questions.
- When reporting results (cron, tasks), lead with the conclusion, not the process.
"""


class ContextBuilder:
    """Builds context prefix for new session's first message."""

    def __init__(self, memory: MemoryStore, config: Config):
        self._memory = memory
        self._config = config

    def _workspace_id(self, platform: str, chat_id: str, project: str = "") -> str:
        """Derive workspace_id from workspace_mode."""
        if project:
            return hashlib.sha256(project.encode()).hexdigest()[:12]
        mode = self._config.get_workspace_mode(platform)
        if mode == "per_chat":
            return "_global"
        cwd = self._config.get_session_cwd(platform, chat_id)
        return hashlib.sha256(cwd.encode()).hexdigest()[:12]

    def _is_first_session(self) -> bool:
        """Check if this is the user's very first conversation."""
        prefs = self._memory.read_preferences()
        return prefs.strip() == _DEFAULT_PREFS.strip()

    def build_message(self, text: str, is_new_session: bool,
                      platform: str = "", chat_id: str = "",
                      project: str = "") -> str:
        """Wrap user message with memory context on new sessions."""
        if not is_new_session:
            return text

        parts: list[str] = []

        # Current date/time
        now = datetime.now(timezone.utc).astimezone()
        parts.append(f"[CURRENT DATE] {now.strftime('%A, %Y-%m-%d %H:%M %Z')}\n")

        # Communication style guide
        parts.append(_COMMUNICATION_STYLE)

        # Optional persona (from ~/.kirocli-gateway/persona.md)
        persona_path = Path(self._config.kiro.gateway_state_dir) / "persona.md"
        if persona_path.exists():
            persona = persona_path.read_text(encoding="utf-8").strip()
            if persona:
                parts.append(f"[Persona]\n{persona}\n")

        # First session bootstrap
        if self._is_first_session():
            parts.append(
                "[FIRST SESSION] This is the user's first conversation with you. "
                "Introduce yourself briefly, ask their name and preferred language, "
                "and what kind of work they'll be doing. Keep it natural and short — "
                "one question at a time.\n"
            )

        # Memory context (global prefs/lessons/history + workspace-scoped projects)
        ws_id = self._workspace_id(platform, chat_id, project) if platform else "_global"
        memory_ctx = self._memory.get_context(workspace_id=ws_id)
        if memory_ctx:
            parts.append(memory_ctx)

        # User message
        parts.append(f"[USER MESSAGE]\n{text}")

        full = "\n".join(parts)
        log.info("[Context] Injected %d chars of context for new session (ws=%s)",
                 len(full) - len(text), ws_id)
        return full

"""Context builder — injects memory into the first message of a new session.

On new sessions (is_new_session=True): prepends date/time + memory context.
On follow-up messages: returns text as-is, trusting ACP native history.
This matches MeshClaw's "context prefix injection" pattern.
"""

import hashlib
import logging
from datetime import datetime, timezone

from config import Config
from memory import MemoryStore

log = logging.getLogger(__name__)


class ContextBuilder:
    """Builds context prefix for new session's first message."""

    def __init__(self, memory: MemoryStore, config: Config):
        self._memory = memory
        self._config = config

    def _workspace_id(self, platform: str, chat_id: str) -> str:
        """Derive workspace_id from workspace_mode.

        per_chat mode: all chats share "_global" (no project concept).
        fixed mode: hash the cwd for a stable, filesystem-safe ID.
        """
        mode = self._config.get_workspace_mode(platform)
        if mode == "per_chat":
            return "_global"
        cwd = self._config.get_session_cwd(platform, chat_id)
        return hashlib.sha256(cwd.encode()).hexdigest()[:12]

    def build_message(self, text: str, is_new_session: bool,
                      platform: str = "", chat_id: str = "") -> str:
        """Wrap user message with memory context on new sessions.

        On follow-up messages, return text as-is (trust ACP native history).
        """
        if not is_new_session:
            return text

        parts: list[str] = []

        # Current date/time — so the LLM knows "today"
        now = datetime.now(timezone.utc).astimezone()
        parts.append(f"[CURRENT DATE] {now.strftime('%A, %Y-%m-%d %H:%M %Z')}\n")

        # Memory context (global prefs/lessons + workspace-scoped projects)
        ws_id = self._workspace_id(platform, chat_id) if platform else "_global"
        memory_ctx = self._memory.get_context(workspace_id=ws_id)
        if memory_ctx:
            parts.append(memory_ctx)

        # User message
        parts.append(f"[USER MESSAGE]\n{text}")

        full = "\n".join(parts)
        log.info("[Context] Injected %d chars of context for new session (ws=%s)",
                 len(full) - len(text), ws_id)
        return full

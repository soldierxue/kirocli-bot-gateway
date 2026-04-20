"""Persistent mapping of chat keys to kiro-cli session IDs.

Enables session resume after kiro-cli restart (idle timeout, crash, LRU eviction,
gateway restart). Uses atomic write (tmp + os.replace) for crash safety.

Storage: ~/.kirocli-gateway/session_map.json
Format:  {"platform:chat_id": {"sid": "kiro-session-id", "mode_id": "agent-name"}}
"""

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

KIRO_SESSIONS_DIR = Path.home() / ".kiro" / "sessions" / "cli"


class SessionMap:
    """Persistent mapping: chat_key → kiro-cli session ID + agent mode."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                log.warning("[SessionMap] Failed to load %s, starting fresh", self._path)
                self._data = {}
        else:
            self._data = {}

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self._path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp_path, str(self._path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get(self, key: str) -> dict | None:
        """Return {"sid": ..., "mode_id": ...} or None.

        Auto-prunes entries whose kiro-cli session files no longer exist on disk.
        Also rejects empty sessions (JSONL < 10 bytes) to prevent false resumes.
        """
        entry = self._data.get(key)
        if not entry:
            return None
        sid = entry.get("sid", "")
        if not sid:
            return None

        session_file = KIRO_SESSIONS_DIR / f"{sid}.json"
        if not session_file.exists():
            log.info("[SessionMap] Session file gone for %s (sid=%s), pruning", key, sid)
            del self._data[key]
            self._save()
            return None

        # Reject empty sessions — kiro-cli created the session but no real
        # conversation happened (e.g. user sent one message then timed out).
        jsonl_file = KIRO_SESSIONS_DIR / f"{sid}.jsonl"
        try:
            if jsonl_file.exists() and jsonl_file.stat().st_size < 10:
                log.info("[SessionMap] Empty JSONL for %s (sid=%s), pruning", key, sid)
                del self._data[key]
                self._save()
                return None
        except OSError:
            pass

        return entry

    def set(self, key: str, sid: str, mode_id: str = ""):
        """Save or update a mapping entry."""
        self._data[key] = {"sid": sid, "mode_id": mode_id}
        self._save()

    def update_mode(self, key: str, mode_id: str):
        """Update agent mode for an existing entry (called on /agent switch)."""
        entry = self._data.get(key)
        if entry:
            entry["mode_id"] = mode_id
            self._save()

    def delete(self, key: str):
        """Remove a mapping entry."""
        if key in self._data:
            del self._data[key]
            self._save()

    def prune(self) -> int:
        """Remove all entries whose kiro-cli session files no longer exist.

        Called at gateway startup to clean up after kiro-cli GC.
        """
        stale = [
            k for k, e in self._data.items()
            if e.get("sid")
            and not (KIRO_SESSIONS_DIR / f"{e['sid']}.json").exists()
        ]
        for k in stale:
            del self._data[k]
        if stale:
            self._save()
            log.info("[SessionMap] Pruned %d stale entries", len(stale))
        return len(stale)

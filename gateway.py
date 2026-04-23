"""Gateway: connects chat adapters to Kiro CLI via ACP protocol.

Platform-agnostic gateway that works with any ChatAdapter implementation.
Each platform gets its own Kiro CLI instance for fault isolation.
workspace_mode only affects session working directories, not Kiro CLI instances.
"""

import base64
import logging
import os
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass

from adapters.base import ChatAdapter, ChatType, IncomingMessage, CardHandle
from acp_client import ACPClient, PromptResult, PermissionRequest
from config import Config
from consolidator import MemoryConsolidator
from context import ContextBuilder
from cron import CronService
from memory import MemoryStore
from session_map import SessionMap
from task_runner import TaskRunner, Task, TaskStep

log = logging.getLogger(__name__)

# Permission request timeout (seconds)
_PERMISSION_TIMEOUT = 60


def format_response(result: PromptResult) -> str:
    """Format Kiro's response with tool call info."""
    parts = []

    # Show tool calls
    for tc in result.tool_calls:
        icon = {"fs": "📄", "edit": "📝", "terminal": "⚡", "other": "🔧"}.get(tc.kind, "🔧")
        if result.stop_reason == "refusal" and tc.status != "completed":
            status_icon = "🚫"
        else:
            status_icon = {"completed": "✅", "failed": "❌"}.get(tc.status, "⏳")
        line = f"{icon} {tc.title} {status_icon}"
        parts.append(line)

    if parts:
        parts.append("")

    if result.stop_reason == "refusal":
        if result.text:
            parts.append(result.text)
        else:
            parts.append("🚫 Operation cancelled")
        parts.append("")
        parts.append("💬 You can continue the conversation")
    elif result.text:
        parts.append(result.text)

    return "\n".join(parts) if parts else "(No response)"


@dataclass
class ChatContext:
    """Context for a chat conversation."""
    chat_id: str
    platform: str
    session_id: str | None = None
    mode_id: str = ""  # Remember agent selection across session_load
    active_project: str = ""  # Current project path (empty = main session)


class Gateway:
    """Platform-agnostic gateway between chat adapters and Kiro CLI.
    
    Each chat gets its own Kiro CLI instance for:
    - Parallel inference across different chats
    - Fault isolation (one crash doesn't affect others)
    - Independent idle timeout per chat
    
    Resource control:
    - max_instances: LRU eviction when exceeded
    - cold_start_limit: limits concurrent kiro-cli startups
    - idle_timeout: auto-stop inactive instances
    
    workspace_mode affects session working directories:
    - fixed: all sessions share the same directory
    - per_chat: each session gets its own subdirectory
    """

    def __init__(self, config: Config, adapters: list[ChatAdapter]):
        self._config = config
        self._adapters = adapters
        self._adapter_map: dict[str, ChatAdapter] = {a.platform_name: a for a in adapters}
        
        # Per-chat ACP clients: "platform:chat_id" -> ACPClient
        self._acp_clients: dict[str, ACPClient] = {}
        self._acp_lock = threading.Lock()
        self._start_sem = threading.Semaphore(config.kiro.cold_start_limit)
        
        # Per-chat last activity time: "platform:chat_id" -> timestamp
        self._last_activity: dict[str, float] = {}
        
        # Chat context: "platform:chat_id" -> ChatContext
        self._contexts: dict[str, ChatContext] = {}
        self._contexts_lock = threading.Lock()
        
        # Processing state: "platform:chat_id" -> True if processing
        self._processing: dict[str, bool] = {}
        self._processing_lock = threading.Lock()
        
        # Pending messages for debounce + collect: key -> [(text, images)]
        self._pending_messages: dict[str, list[tuple[str, list | None]]] = {}
        self._pending_lock = threading.Lock()
        # Reply target: key -> message_id (for group chat reply, feishu only)
        self._reply_targets: dict[str, str] = {}
        self._debounce_timers: dict[str, threading.Timer] = {}
        self._DEBOUNCE_BY_PLATFORM = {
            "discord": config.debounce_discord,
            "feishu": config.debounce_feishu,
        }
        self._DEBOUNCE_DEFAULT = config.debounce_default
        self._PENDING_CAP = config.pending_cap
        
        # Pending permission requests: "platform:chat_id" -> (event, result_holder)
        self._pending_permissions: dict[str, tuple[threading.Event, list]] = {}
        self._pending_permissions_lock = threading.Lock()
        
        # Active card handles: "platform:chat_id" -> CardHandle (for permission UI reuse)
        self._active_cards: dict[str, CardHandle] = {}
        
        # session_id -> "platform:chat_id" mapping
        self._session_to_key: dict[str, str] = {}
        
        # Session resume: persistent mapping of chat key → kiro-cli session ID
        self._session_map = SessionMap(
            path=os.path.join(config.kiro.gateway_state_dir, "session_map.json")
        )
        
        # Memory system: persistent user knowledge across all sessions
        self._memory = MemoryStore(base_dir=config.kiro.gateway_state_dir)
        self._memory.init()
        self._ctx_builder = ContextBuilder(memory=self._memory, config=config)
        self._consolidator = MemoryConsolidator(memory=self._memory)
        
        # Background kiro-cli: persistent instance for consolidation, cron, tasks
        self._bg_acp: ACPClient | None = None
        self._bg_session_id: str | None = None
        self._bg_lock = threading.Lock()
        
        # Cron service
        self._cron = CronService(
            state_dir=config.kiro.gateway_state_dir,
            send_callback=lambda p, c, t: self._send_text_nowait(p, c, t),
            heartbeat_enabled=config.kiro.heartbeat_enabled,
            heartbeat_interval=config.kiro.heartbeat_interval,
            heartbeat_target=config.kiro.heartbeat_target,
            heartbeat_exclude=config.kiro.heartbeat_exclude,
        )
        
        # Task runner
        self._task_runner = TaskRunner(
            cli_path=config.kiro.path,
            default_cwd=config.kiro.default_cwd or os.getcwd(),
            send_callback=lambda p, c, t: self._send_text_nowait(p, c, t),
        )
        # Pending task confirmation: key -> Task (waiting for "go")
        self._pending_tasks: dict[str, Task] = {}
        
        # Idle checker
        self._idle_checker_stop = threading.Event()
        self._idle_checker_thread: threading.Thread | None = None

    def _make_key(self, platform: str, chat_id: str) -> str:
        """Create unique key for platform:chat_id combination."""
        return f"{platform}:{chat_id}"

    def _make_project_key(self, platform: str, chat_id: str, project: str) -> str:
        """Create key for a project-specific kiro-cli instance."""
        base = self._make_key(platform, chat_id)
        return f"{base}@{project}" if project else base

    # ── Background kiro-cli ──

    def _start_background(self):
        """Start the persistent background kiro-cli instance."""
        log.info("[Gateway] Starting background kiro-cli...")
        self._bg_acp = ACPClient(cli_path=self._config.kiro.path)
        cwd = self._config.kiro.default_cwd or os.getcwd()
        self._bg_acp.start(cwd=cwd)
        self._bg_session_id, _ = self._bg_acp.session_new(cwd)
        # Background tasks auto-approve (no interactive user)
        log.info("[Gateway] Background kiro-cli ready (session=%s)", self._bg_session_id)

    def _ensure_background(self):
        """Ensure background kiro-cli is running, restart if dead."""
        if self._bg_acp and self._bg_acp.is_running() and self._bg_session_id:
            return
        log.info("[Gateway] Background kiro-cli not running, restarting...")
        if self._bg_acp:
            try:
                self._bg_acp.stop()
            except Exception:
                pass
        self._start_background()

    def _recycle_background(self):
        """Recycle background kiro-cli if context usage is high."""
        if not self._bg_acp or not self._bg_session_id:
            return
        usage = self._bg_acp.get_context_usage(self._bg_session_id)
        if usage >= 70:
            log.info("[Gateway] Recycling background kiro-cli (usage=%.0f%%)", usage)
            self._bg_acp.stop()
            self._start_background()

    def start(self):
        """Start the gateway and all adapters."""
        log.info("[Gateway] Starting with per-chat Kiro CLI instances (workspace_mode=%s, max=%d)", 
                 self._config.kiro.workspace_mode, self._config.kiro.max_instances)

        # Prune stale session map entries from previous runs
        self._session_map.prune()

        # Start background kiro-cli for consolidation, cron, and tasks
        try:
            self._start_background()
        except Exception:
            log.warning("[Gateway] Background kiro-cli failed to start", exc_info=True)

        # Wire cron execution to background kiro-cli
        def _cron_execute(job):
            self._ensure_background()
            with self._bg_lock:
                result = self._bg_acp.session_prompt(self._bg_session_id, job.message)
            self._recycle_background()
            from gateway import format_response
            return format_response(result)
        self._cron.execute_callback = _cron_execute
        self._cron.start()

        # Start idle checker
        self._idle_checker_stop.clear()
        self._idle_checker_thread = threading.Thread(target=self._idle_checker_loop, daemon=True)
        self._idle_checker_thread.start()

        # Setup graceful shutdown
        def shutdown(sig, frame):
            log.info("[Gateway] Shutting down...")
            self._idle_checker_stop.set()
            self._cron.stop()
            # Cancel all debounce timers
            with self._pending_lock:
                for timer in self._debounce_timers.values():
                    timer.cancel()
                self._debounce_timers.clear()
            self._stop_all_acp()
            # Stop background kiro-cli
            if self._bg_acp:
                try:
                    self._bg_acp.stop()
                except Exception:
                    pass
            for adapter in self._adapters:
                adapter.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        # Start adapters
        if not self._adapters:
            log.error("[Gateway] No adapters configured")
            return

        # Setup slash command handler for Discord adapter
        for adapter in self._adapters:
            if adapter.platform_name == "discord" and hasattr(adapter, "set_slash_handler"):
                adapter.set_slash_handler(self._handle_slash_command)
                log.info("[Gateway] Slash command handler set for Discord")

        # Start all but last adapter in threads
        for adapter in self._adapters[:-1]:
            log.info("[Gateway] Starting %s adapter in thread...", adapter.platform_name)
            t = threading.Thread(
                target=adapter.start,
                args=(self._on_message,),
                daemon=True,
            )
            t.start()

        # Start last adapter in main thread (blocking)
        last_adapter = self._adapters[-1]
        log.info("[Gateway] Starting %s adapter (blocking)...", last_adapter.platform_name)
        last_adapter.start(self._on_message)

    def _start_acp(self, platform: str, chat_id: str, project: str = "") -> ACPClient:
        """Start ACP client for a specific chat if not running.
        
        When project is specified, the kiro-cli instance uses the project path
        as cwd (loading project-level .kiro/ config).
        
        Limits concurrent cold starts via _start_sem.
        Evicts LRU instance if max_instances is reached.
        """
        key = self._make_project_key(platform, chat_id, project)

        # Fast path: already running
        with self._acp_lock:
            acp = self._acp_clients.get(key)
            if acp and acp.is_running():
                self._last_activity[key] = time.time()
                return acp
            # Clean up dead process reference
            if acp and not acp.is_running():
                self._acp_clients.pop(key, None)
                self._last_activity.pop(key, None)

        # Slow path: cold start (outside lock to avoid blocking other chats)
        self._start_sem.acquire()
        try:
            # Double-check after acquiring semaphore
            with self._acp_lock:
                acp = self._acp_clients.get(key)
                if acp and acp.is_running():
                    self._last_activity[key] = time.time()
                    return acp

                # LRU eviction if at capacity
                running = sum(1 for a in self._acp_clients.values() if a.is_running())
                if running >= self._config.kiro.max_instances and self._last_activity:
                    oldest_key = min(self._last_activity, key=self._last_activity.get)
                    log.info("[Gateway] Instance limit (%d) reached, evicting %s",
                             self._config.kiro.max_instances, oldest_key)
                    evict_key = oldest_key
                else:
                    evict_key = None

            if evict_key:
                self._stop_acp_by_key(evict_key)

            log.info("[Gateway] [%s] Starting kiro-cli...", key)
            acp = ACPClient(cli_path=self._config.kiro.path)

            # Project sessions use the project path as cwd directly.
            # Main sessions use workspace_mode logic (fixed → platform cwd, per_chat → None).
            if project:
                cwd = project
            else:
                cwd = self._config.get_kiro_cwd(platform)
            acp.start(cwd=cwd)
            if not self._config.kiro.auto_approve:
                acp.on_permission_request(lambda req, p=platform: self._handle_permission(req, p))
            else:
                log.info("[Gateway] [%s] Auto-approve enabled, skipping permission handler", key)

            with self._acp_lock:
                self._acp_clients[key] = acp
                self._last_activity[key] = time.time()

            mode = self._config.get_workspace_mode(platform)
            log.info("[Gateway] [%s] kiro-cli started (mode=%s, cwd=%s)", key, mode, cwd)
            return acp
        finally:
            self._start_sem.release()

    def _stop_acp_by_key(self, key: str):
        """Stop ACP client for a specific chat key."""
        with self._acp_lock:
            acp = self._acp_clients.pop(key, None)
            self._last_activity.pop(key, None)

        if acp is not None:
            log.info("[Gateway] [%s] Stopping kiro-cli...", key)
            acp.stop()

            # Clear session context for this chat
            with self._contexts_lock:
                ctx = self._contexts.pop(key, None)
                if ctx:
                    if ctx.session_id:
                        self._session_to_key.pop(ctx.session_id, None)
                    platform, chat_id = key.split(":", 1)
                    self._cleanup_images(platform, chat_id)

            log.info("[Gateway] [%s] kiro-cli stopped", key)

    def _stop_all_acp(self):
        """Stop all ACP clients."""
        with self._acp_lock:
            keys = list(self._acp_clients.keys())
        for key in keys:
            self._stop_acp_by_key(key)

    def _ensure_acp(self, platform: str, chat_id: str, project: str = "") -> ACPClient:
        """Ensure ACP client is running for a chat (or project session)."""
        return self._start_acp(platform, chat_id, project=project)

    def _get_acp(self, platform: str, chat_id: str = "") -> ACPClient | None:
        """Get ACP client for a chat if running.
        
        When chat_id is provided, looks up the per-chat instance.
        When chat_id is empty, searches for any running instance for the platform
        (used by slash commands that don't have chat context yet).
        """
        with self._acp_lock:
            if chat_id:
                key = self._make_key(platform, chat_id)
                acp = self._acp_clients.get(key)
                if acp and acp.is_running():
                    return acp
            else:
                # Fallback: find any running instance for this platform
                for k, acp in self._acp_clients.items():
                    if k.startswith(f"{platform}:") and acp.is_running():
                        return acp
        return None

    def _idle_checker_loop(self):
        """Background thread for per-chat idle timeout, memory consolidation, and config hot-reload."""
        idle_timeout = self._config.kiro.idle_timeout
        if idle_timeout <= 0:
            log.info("[Gateway] Idle timeout disabled")
            return
        
        _CONSOLIDATION_IDLE = 60  # seconds idle before triggering consolidation
        _config_mtime: float = 0.0
        
        while not self._idle_checker_stop.wait(timeout=30):
            # Config hot-reload: check .env mtime
            try:
                env_path = Path(".env")
                if env_path.exists():
                    mtime = env_path.stat().st_mtime
                    if mtime > _config_mtime and _config_mtime > 0:
                        self._hot_reload_config()
                    _config_mtime = mtime
            except Exception:
                pass
            
            idle_timeout = self._config.kiro.idle_timeout  # May have been hot-reloaded
            keys_to_stop = []
            keys_to_consolidate = []
            
            with self._acp_lock:
                now = time.time()
                for key, last in list(self._last_activity.items()):
                    idle_time = now - last
                    acp = self._acp_clients.get(key)
                    if not acp or not acp.is_running():
                        continue
                    
                    if idle_time > idle_timeout:
                        log.info("[Gateway] [%s] Idle timeout (%.0fs)", key, idle_time)
                        keys_to_stop.append(key)
                    elif idle_time > _CONSOLIDATION_IDLE:
                        if self._consolidator.should_consolidate(key):
                            keys_to_consolidate.append(key)
            
            # Trigger consolidation (before stopping — needs running kiro-cli)
            for key in keys_to_consolidate:
                self._consolidator.mark_running(key)
                threading.Thread(
                    target=self._run_consolidation,
                    args=(key,),
                    daemon=True,
                ).start()
            
            # Stop idle instances
            for key in keys_to_stop:
                self._stop_acp_by_key(key)

    def _hot_reload_config(self):
        """Reload safe config values from .env without restarting."""
        try:
            from dotenv import load_dotenv
            load_dotenv(override=True)
            
            new_level = os.getenv("LOG_LEVEL", "INFO")
            logging.getLogger().setLevel(getattr(logging, new_level.upper(), logging.INFO))
            
            self._config.kiro.idle_timeout = int(os.getenv("KIRO_IDLE_TIMEOUT", "300"))
            self._config.kiro.fallback_model = os.getenv("KIRO_FALLBACK_MODEL", "")
            self._config.debounce_discord = float(os.getenv("DEBOUNCE_DISCORD", "1.5"))
            self._config.debounce_feishu = float(os.getenv("DEBOUNCE_FEISHU", "1.0"))
            
            log.info("[Gateway] Config hot-reloaded from .env")
        except Exception as e:
            log.warning("[Gateway] Config hot-reload failed: %s", e)

    def _run_consolidation(self, effective_key: str):
        """Run memory consolidation using the background kiro-cli.
        
        Reads recent conversation from kiro-cli's JSONL, sends a consolidation
        prompt to the background kiro-cli, and writes extracted knowledge to memory.
        """
        # Parse platform:chat_id from effective_key
        base_key = effective_key.split("@")[0]
        parts = base_key.split(":", 1)
        if len(parts) != 2:
            self._consolidator.mark_done(effective_key)
            return
        platform, chat_id = parts

        # Get session_id for reading conversation history
        with self._contexts_lock:
            ctx = self._contexts.get(effective_key) or self._contexts.get(base_key)
            session_id = ctx.session_id if ctx else None
            active_project = ctx.active_project if ctx else ""

        if not session_id:
            self._consolidator.mark_done(effective_key)
            return

        # Read recent conversation
        conversation = self._consolidator.read_recent_conversation(session_id)
        if not conversation:
            log.info("[Gateway] [%s] No conversation to consolidate", effective_key)
            self._consolidator.mark_done(effective_key)
            return

        ws_id = self._ctx_builder._workspace_id(platform, chat_id, active_project)

        # Notify user
        self._send_text_nowait(platform, chat_id,
                               "🧠 Analyzing conversation to update memory...")

        try:
            # Use background kiro-cli (not the user's chat kiro-cli)
            self._ensure_background()
            prompt = self._consolidator.build_prompt(conversation, workspace_id=ws_id)
            with self._bg_lock:
                result = self._bg_acp.session_prompt(self._bg_session_id, prompt)
            self._recycle_background()

            if result.text:
                changed = self._consolidator.apply_result(result.text, workspace_id=ws_id)
                if changed:
                    self._send_text_nowait(platform, chat_id,
                                           "🧠 Memory updated from conversation ✅")
                    log.info("[Gateway] [%s] Memory consolidation succeeded", effective_key)
                else:
                    log.info("[Gateway] [%s] Consolidation found nothing new", effective_key)
            else:
                log.warning("[Gateway] [%s] Consolidation returned empty response", effective_key)
        except Exception as e:
            log.warning("[Gateway] [%s] Consolidation failed: %s", effective_key, e)
        finally:
            self._consolidator.mark_done(effective_key)

    def _get_adapter(self, platform: str) -> ChatAdapter | None:
        """Get adapter by platform name."""
        return self._adapter_map.get(platform)

    def _send_text(self, platform: str, chat_id: str, text: str, reply_to: str = ""):
        """Send text message via appropriate adapter."""
        adapter = self._get_adapter(platform)
        if adapter:
            adapter.send_text(chat_id, text, reply_to=reply_to)

    def _send_text_nowait(self, platform: str, chat_id: str, text: str):
        """Send text message without blocking (for command responses).
        
        Falls back to send_text if adapter doesn't support nowait.
        """
        adapter = self._get_adapter(platform)
        if adapter:
            if hasattr(adapter, 'send_text_nowait'):
                adapter.send_text_nowait(chat_id, text)
            else:
                adapter.send_text(chat_id, text)

    def _send_card(self, platform: str, chat_id: str, content: str, title: str = "", reply_to: str = "") -> CardHandle | None:
        """Send card via appropriate adapter."""
        adapter = self._get_adapter(platform)
        if adapter:
            return adapter.send_card(chat_id, content, title, reply_to=reply_to)
        return None

    def _update_card(self, platform: str, handle: CardHandle, content: str, title: str = "") -> bool:
        """Update card via appropriate adapter."""
        adapter = self._get_adapter(platform)
        if adapter:
            return adapter.update_card(handle, content, title)
        return False

    def _handle_permission(self, request: PermissionRequest, platform: str) -> str | None:
        """Handle permission request from Kiro."""
        session_id = request.session_id
        key = self._session_to_key.get(session_id)
        if not key:
            log.warning("[Gateway] [%s] No chat found for session %s, auto-denying", platform, session_id)
            return "deny"

        _, chat_id = key.split(":", 1)
        
        msg = f"🔐 **Kiro requests permission:**\n\n"
        msg += f"📋 {request.title}\n\n"
        msg += "Reply: **y**(allow) / **n**(deny) / **t**(trust)\n"
        msg += f"⏱️ Auto-deny in {_PERMISSION_TIMEOUT}s"

        # Prefer updating the active card (Feishu) over sending a new message (Discord)
        card = self._active_cards.get(key)
        if card:
            self._update_card(platform, card, msg)
        else:
            self._send_text(platform, chat_id, msg)
        log.info("[Gateway] [%s] Sent permission request: %s", platform, request.title)

        evt = threading.Event()
        result_holder: list = []

        with self._pending_permissions_lock:
            self._pending_permissions[key] = (evt, result_holder)

        try:
            if evt.wait(timeout=_PERMISSION_TIMEOUT):
                if result_holder:
                    decision = result_holder[0]
                    log.info("[Gateway] [%s] User decision: %s", platform, decision)
                    # Send new card below user's reply for the result
                    if card:
                        new_card = self._send_card(platform, chat_id, "🤔 Processing...")
                        if new_card:
                            self._active_cards[key] = new_card
                    return decision
            
            # Timeout
            if card:
                self._update_card(platform, card, "⏱️ Timeout, auto-denied")
            else:
                self._send_text(platform, chat_id, "⏱️ Timeout, auto-denied")
            log.warning("[Gateway] [%s] Permission timed out: %s", platform, request.title)
            return "deny"
        finally:
            with self._pending_permissions_lock:
                self._pending_permissions.pop(key, None)

    def _on_message(self, msg: IncomingMessage):
        """Handle incoming message from any adapter."""
        platform = msg.raw.get("_platform", "")
        if not platform:
            log.warning("[Gateway] Message missing _platform in raw data")
            if self._adapters:
                platform = self._adapters[0].platform_name
            else:
                return
        
        chat_id = msg.chat_id
        text = msg.text.strip()
        text_lower = text.lower()
        images = msg.images
        key = self._make_key(platform, chat_id)

        if images:
            log.info("[Gateway] [%s] Received %d image(s)", key, len(images))

        # Check for permission response
        with self._pending_permissions_lock:
            pending = self._pending_permissions.get(key)
        
        if pending:
            evt, result_holder = pending
            if text_lower in ('y', 'yes', 'ok'):
                result_holder.append("allow_once")
                evt.set()
                return
            elif text_lower in ('n', 'no'):
                result_holder.append("deny")
                evt.set()
                return
            elif text_lower in ('t', 'trust', 'always'):
                result_holder.append("allow_always")
                evt.set()
                return
            else:
                self._send_text_nowait(platform, chat_id, "⚠️ Please reply y/n/t")
                return

        # Cancel command
        if text_lower in ("cancel", "stop"):
            self._handle_cancel(platform, chat_id, key)
            return

        # Task confirmation ("go" to start a pending task)
        if text_lower == "go" and key in self._pending_tasks:
            task = self._pending_tasks.pop(key)
            self._send_text_nowait(platform, chat_id, "🚀 Starting task...")
            threading.Thread(
                target=self._task_runner.run,
                args=(task, self._bg_acp, self._bg_session_id),
                daemon=True,
            ).start()
            return

        # Commands (/ prefix)
        if text.startswith("/"):
            self._handle_command(platform, chat_id, key, text)
            return

        # Store in pending buffer for debounce + collect
        # Save last message_id for group chat reply (feishu + discord)
        if msg.chat_type == ChatType.GROUP:
            raw_msg_id = msg.raw.get("message_id", "")
            if raw_msg_id:
                self._reply_targets[key] = raw_msg_id
        with self._pending_lock:
            if key not in self._pending_messages:
                self._pending_messages[key] = []
            pending = self._pending_messages[key]
            if len(pending) >= self._PENDING_CAP:
                self._send_text_nowait(platform, chat_id,
                                       f"⚠️ Too many pending messages (max {self._PENDING_CAP})")
                return
            pending.append((text, images))

        with self._processing_lock:
            is_busy = self._processing.get(key, False)

        if is_busy:
            # Currently processing — send typing indicator, loop will drain pending
            adapter = self._adapter_map.get(platform)
            if adapter:
                adapter.send_typing(chat_id)
        else:
            # Idle — send immediate typing feedback, then start/reset debounce
            adapter = self._adapter_map.get(platform)
            if adapter:
                adapter.send_typing(chat_id)
            self._reset_debounce(platform, chat_id, key)

    def _handle_cancel(self, platform: str, chat_id: str, key: str):
        """Handle cancel command.
        
        Uses _send_text_nowait to avoid deadlocking Discord's event loop
        (this is called synchronously from the adapter's message handler).
        """
        # Cancel debounce timer and clear pending messages
        pending_cleared = 0
        with self._pending_lock:
            timer = self._debounce_timers.pop(key, None)
            if timer:
                timer.cancel()
            pending_cleared = len(self._pending_messages.pop(key, []))
        
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            session_id = ctx.session_id if ctx else None

        if not session_id:
            if pending_cleared:
                self._send_text_nowait(platform, chat_id, f"🗑️ Cleared {pending_cleared} queued message(s)")
            else:
                self._send_text_nowait(platform, chat_id, "❌ No active session")
            return

        acp = self._get_acp(platform, chat_id)
        if not acp:
            if pending_cleared:
                self._send_text_nowait(platform, chat_id, f"🗑️ Cleared {pending_cleared} queued message(s)")
            else:
                self._send_text_nowait(platform, chat_id, "❌ Kiro is not running")
            return

        try:
            acp.session_cancel(session_id)
            msg = "⏹️ Cancel request sent"
            if pending_cleared:
                msg += f"\n🗑️ Cleared {pending_cleared} queued message(s)"
            self._send_text_nowait(platform, chat_id, msg)
        except Exception as e:
            log.error("[Gateway] [%s] Cancel failed: %s", key, e)
            self._send_text_nowait(platform, chat_id, f"❌ Cancel failed: {e}")

    def _handle_command(self, platform: str, chat_id: str, key: str, text: str):
        """Handle slash commands."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/agent":
            self._handle_agent_command(platform, chat_id, key, arg)
        elif cmd == "/model":
            self._handle_model_command(platform, chat_id, key, arg)
        elif cmd == "/project":
            self._handle_project_command(platform, chat_id, key, arg)
        elif cmd == "/remember":
            self._handle_remember_command(platform, chat_id, arg)
        elif cmd == "/forget":
            self._handle_forget_command(platform, chat_id, arg)
        elif cmd == "/memory":
            self._handle_memory_command(platform, chat_id)
        elif cmd == "/cron":
            self._handle_cron_command(platform, chat_id, key, arg)
        elif cmd == "/task":
            self._handle_task_command(platform, chat_id, key, arg)
        elif cmd == "/cli":
            self._handle_cli_command(platform, chat_id, arg)
        elif cmd == "/help":
            self._handle_help_command(platform, chat_id)
        else:
            # Unknown gateway command → try forwarding to kiro-cli
            self._handle_kiro_command(platform, chat_id, key, text)

    def _handle_agent_command(self, platform: str, chat_id: str, key: str, mode_arg: str):
        """Handle /agent command (text-based)."""
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            session_id = ctx.session_id if ctx else None

        acp = self._get_acp(platform, chat_id)
        
        # Auto-start session if needed (user may not have sent a message yet)
        if not session_id and not acp:
            try:
                acp = self._ensure_acp(platform, chat_id)
                session_id, _ = self._get_or_create_session(platform, chat_id, key, acp)
            except Exception:
                pass

        response = self._get_agent_response(acp, session_id, mode_arg)
        self._send_text_nowait(platform, chat_id, response)

    def _handle_model_command(self, platform: str, chat_id: str, key: str, model_arg: str):
        """Handle /model command (text-based)."""
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            session_id = ctx.session_id if ctx else None

        acp = self._get_acp(platform, chat_id)
        
        # Auto-start session if needed (user may not have sent a message yet)
        if not session_id and not acp:
            try:
                acp = self._ensure_acp(platform, chat_id)
                session_id, _ = self._get_or_create_session(platform, chat_id, key, acp)
            except Exception:
                pass

        response = self._get_model_response(acp, session_id, model_arg)
        self._send_text_nowait(platform, chat_id, response)

    def _handle_help_command(self, platform: str, chat_id: str):
        """Show help."""
        self._send_text_nowait(platform, chat_id, self._get_help_text())

    def _handle_cli_command(self, platform: str, chat_id: str, arg: str):
        """Route /cli subcommands."""
        sub = arg.strip().lower() if arg else "status"
        if sub == "status":
            self._handle_cli_status(platform, chat_id)
        elif sub == "restart":
            self._handle_cli_restart(platform, chat_id)
        else:
            self._send_text_nowait(platform, chat_id, "💡 Usage: /cli status | /cli restart")

    def _handle_cli_status(self, platform: str, chat_id: str):
        """Handle /cli status — show all active kiro-cli instances."""
        lines = ["🖥️ **Kiro CLI Instances:**\n"]

        # Background kiro-cli
        bg_status = "🟢 running" if (self._bg_acp and self._bg_acp.is_running()) else "🔴 stopped"
        bg_usage = ""
        if self._bg_acp and self._bg_session_id:
            pct = self._bg_acp.get_context_usage(self._bg_session_id)
            if pct > 0:
                bg_usage = f" (ctx: {pct:.0f}%)"
        lines.append(f"**Background** (_bg): {bg_status}{bg_usage}")

        # Per-chat instances
        with self._acp_lock:
            chat_keys = sorted(self._acp_clients.keys())
            running_count = sum(1 for a in self._acp_clients.values() if a.is_running())

        if chat_keys:
            lines.append(f"\n**Chat instances** ({running_count} running, "
                         f"max {self._config.kiro.max_instances}):\n")
            for acp_key in chat_keys:
                with self._acp_lock:
                    acp = self._acp_clients.get(acp_key)
                    last = self._last_activity.get(acp_key, 0)

                if not acp:
                    continue

                status = "🟢" if acp.is_running() else "🔴"
                idle = time.time() - last if last else 0

                # Get context usage and session info
                ctx_info = ""
                with self._contexts_lock:
                    ctx = self._contexts.get(acp_key)
                    sid = ctx.session_id if ctx else None
                if sid:
                    pct = acp.get_context_usage(sid)
                    if pct > 0:
                        ctx_info = f" ctx:{pct:.0f}%"

                # Format key for display
                display = acp_key
                if "@" in display:
                    base, proj = display.split("@", 1)
                    display = f"{base} → 📂 {os.path.basename(proj)}"

                lines.append(f"  {status} `{display}` idle:{idle:.0f}s{ctx_info}")
        else:
            lines.append("\nNo chat instances running.")

        # Cron jobs
        cron_jobs = self._cron.list_jobs()
        if cron_jobs:
            active = sum(1 for j in cron_jobs if not j.paused)
            lines.append(f"\n**Cron jobs**: {active} active / {len(cron_jobs)} total")

        # Active task
        task = self._task_runner.active_task
        if task:
            ok = sum(1 for s in task.steps if s.status == "ok")
            lines.append(f"\n**Task**: {task.description[:50]}... ({ok}/{len(task.steps)} steps)")

        lines.append(f"\n**Model**: {self._config.kiro.default_model}")
        lines.append(f"**Auto-approve**: {'✅ on' if self._config.kiro.auto_approve else '❌ off'}")

        self._send_text_nowait(platform, chat_id, "\n".join(lines))

    def _handle_cli_restart(self, platform: str, chat_id: str):
        """Handle /cli restart — stop and restart all kiro-cli instances."""
        self._send_text_nowait(platform, chat_id, "🔄 Restarting all kiro-cli instances...")

        # Count before
        with self._acp_lock:
            chat_count = sum(1 for a in self._acp_clients.values() if a.is_running())
        has_bg = self._bg_acp and self._bg_acp.is_running()

        # Stop all chat instances (session_map preserved for resume)
        self._stop_all_acp()

        # Restart background
        try:
            if self._bg_acp:
                self._bg_acp.stop()
            self._start_background()
            bg_ok = True
        except Exception as e:
            log.warning("[Gateway] Background restart failed: %s", e)
            bg_ok = False

        msg = f"✅ Restart complete\n"
        msg += f"  Chat instances stopped: {chat_count} (will cold-start on next message)\n"
        msg += f"  Background: {'🟢 restarted' if bg_ok else '🔴 failed'}"
        self._send_text_nowait(platform, chat_id, msg)

    def _handle_kiro_command(self, platform: str, chat_id: str, key: str, text: str):
        """Forward an unrecognized slash command to kiro-cli for execution.
        
        Supports kiro-cli native commands like /compact, /usage, /tools, /mcp, etc.
        """
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            session_id = ctx.session_id if ctx else None
            active_project = ctx.active_project if ctx else ""

        if not session_id:
            self._send_text_nowait(platform, chat_id,
                                   "❌ No active session. Send a message first.")
            return

        effective_key = self._make_project_key(platform, chat_id, active_project)
        acp = self._get_acp(platform, chat_id) if not active_project else None
        if not acp:
            # Try project-specific ACP
            with self._acp_lock:
                acp = self._acp_clients.get(effective_key)
                if acp and not acp.is_running():
                    acp = None
        if not acp:
            self._send_text_nowait(platform, chat_id, "❌ Kiro is not running")
            return

        try:
            output = acp.execute_command(session_id, text)
            label = ""
            if active_project:
                label = f"📂 **[{os.path.basename(active_project)}]** "
            if output:
                self._send_text_nowait(platform, chat_id, f"{label}{output}")
            else:
                self._send_text_nowait(platform, chat_id, f"{label}✓ Done")
        except Exception as e:
            self._send_text_nowait(platform, chat_id, f"❌ Command failed: {e}")

    # ── Cron commands ──

    def _handle_cron_command(self, platform: str, chat_id: str, key: str, arg: str):
        """Route /cron subcommands."""
        parts = arg.split(maxsplit=1) if arg else ["list"]
        sub = parts[0].lower()
        sub_arg = parts[1].strip() if len(parts) > 1 else ""

        if sub == "list" or sub == "ls":
            jobs = self._cron.list_jobs()
            if not jobs:
                self._send_text_nowait(platform, chat_id, "⏰ No cron jobs.\n💡 /cron add \"name\" \"message\" --every 3600")
                return
            lines = ["⏰ **Cron jobs:**\n"]
            for j in jobs:
                status = "⏸️" if j.paused else "🟢"
                lines.append(f"  {status} `{j.id}` **{j.name}** — every {j.interval_secs}s")
            self._send_text_nowait(platform, chat_id, "\n".join(lines))

        elif sub == "add":
            self._handle_cron_add(platform, chat_id, key, sub_arg)

        elif sub == "remove" or sub == "rm":
            if self._cron.remove(sub_arg):
                self._send_text_nowait(platform, chat_id, f"✅ Removed cron job: {sub_arg}")
            else:
                self._send_text_nowait(platform, chat_id, f"❌ Job not found: {sub_arg}")

        elif sub == "pause":
            if self._cron.pause(sub_arg):
                self._send_text_nowait(platform, chat_id, f"⏸️ Paused: {sub_arg}")
            else:
                self._send_text_nowait(platform, chat_id, f"❌ Job not found: {sub_arg}")

        elif sub == "resume":
            if self._cron.resume(sub_arg):
                self._send_text_nowait(platform, chat_id, f"▶️ Resumed: {sub_arg}")
            else:
                self._send_text_nowait(platform, chat_id, f"❌ Job not found: {sub_arg}")

        else:
            self._send_text_nowait(platform, chat_id,
                                   "💡 Usage: /cron add|list|remove|pause|resume")

    def _handle_cron_add(self, platform: str, chat_id: str, key: str, arg: str):
        """Parse /cron add 'name' 'message' --every N or --schedule '0 9 * * 1-5'"""
        import shlex
        try:
            tokens = shlex.split(arg)
        except ValueError:
            self._send_text_nowait(platform, chat_id,
                                   '💡 Usage: /cron add "name" "message" --every 3600\n'
                                   '   or: /cron add "name" "message" --schedule "0 9 * * 1-5"')
            return

        if len(tokens) < 2:
            self._send_text_nowait(platform, chat_id,
                                   '💡 Usage: /cron add "name" "message" --every 3600\n'
                                   '   or: /cron add "name" "message" --schedule "0 9 * * 1-5"')
            return

        name = tokens[0]
        message = tokens[1]
        interval = 0
        schedule = ""

        for i, t in enumerate(tokens):
            if t == "--every" and i + 1 < len(tokens):
                try:
                    interval = int(tokens[i + 1])
                except ValueError:
                    pass
            elif t == "--schedule" and i + 1 < len(tokens):
                schedule = tokens[i + 1]

        if not interval and not schedule:
            interval = 3600  # default 1 hour

        with self._contexts_lock:
            ctx = self._contexts.get(key)
            project = ctx.active_project if ctx else ""

        job = self._cron.add(name, message, interval_secs=interval, schedule=schedule,
                             platform=platform, chat_id=chat_id, project=project)
        sched_info = f'schedule="{schedule}"' if schedule else f"every {interval}s"
        self._send_text_nowait(platform, chat_id,
                               f"✅ Cron job added: **{name}** (id: `{job.id}`, {sched_info})")

    # ── Task commands ──

    def _handle_task_command(self, platform: str, chat_id: str, key: str, arg: str):
        """Route /task subcommands."""
        parts = arg.split(maxsplit=1) if arg else ["status"]
        sub = parts[0].lower()
        sub_arg = parts[1].strip() if len(parts) > 1 else ""

        if sub == "run":
            self._handle_task_run(platform, chat_id, key, sub_arg)
        elif sub == "status":
            task = self._task_runner.active_task
            if task:
                ok = sum(1 for s in task.steps if s.status == "ok")
                self._send_text_nowait(platform, chat_id,
                                       f"📋 Task: {task.description[:60]}\n"
                                       f"  Status: {task.status}\n"
                                       f"  Progress: {ok}/{len(task.steps)} steps")
            else:
                self._send_text_nowait(platform, chat_id, "📋 No active task")
        elif sub == "cancel":
            self._task_runner.cancel()
            self._send_text_nowait(platform, chat_id, "⏹️ Task cancel requested")
        else:
            self._send_text_nowait(platform, chat_id,
                                   "💡 Usage: /task run <description> | /task status | /task cancel")

    def _handle_task_run(self, platform: str, chat_id: str, key: str, description: str):
        """Handle /task run <description> — decompose and confirm."""
        if not description:
            self._send_text_nowait(platform, chat_id, "💡 Usage: /task run <task description>")
            return

        if self._task_runner.active_task:
            self._send_text_nowait(platform, chat_id,
                                   "❌ A task is already running. Use /task cancel first.")
            return

        self._send_text_nowait(platform, chat_id, "🤔 Decomposing task into steps...")

        try:
            self._ensure_background()
            with self._bg_lock:
                steps = self._task_runner.decompose(
                    self._bg_acp, self._bg_session_id, description
                )
            self._recycle_background()
        except Exception as e:
            self._send_text_nowait(platform, chat_id, f"❌ Decomposition failed: {e}")
            return

        if not steps:
            self._send_text_nowait(platform, chat_id, "❌ Failed to decompose task into steps")
            return

        with self._contexts_lock:
            ctx = self._contexts.get(key)
            project = ctx.active_project if ctx else ""

        import uuid as _uuid
        task = Task(
            id=_uuid.uuid4().hex[:8],
            description=description,
            steps=steps,
            status="waiting",
            platform=platform,
            chat_id=chat_id,
            project=project,
        )

        # Show plan and wait for confirmation
        plan_text = self._task_runner.format_plan(task)
        self._send_text_nowait(platform, chat_id, plan_text)
        self._pending_tasks[key] = task

    def _handle_remember_command(self, platform: str, chat_id: str, arg: str):
        """Handle /remember <text> — save a lesson to persistent memory."""
        if not arg:
            self._send_text_nowait(platform, chat_id,
                                   "💡 Usage: /remember <something to remember>\n"
                                   "Example: /remember I prefer pytest-asyncio strict mode")
            return
        self._memory.add_lesson(arg)
        self._send_text_nowait(platform, chat_id, f"✅ Remembered: {arg}")

    def _handle_forget_command(self, platform: str, chat_id: str, arg: str):
        """Handle /forget <keyword> — remove matching lessons from memory."""
        if not arg:
            self._send_text_nowait(platform, chat_id,
                                   "💡 Usage: /forget <keyword>\n"
                                   "Removes all lessons containing the keyword")
            return
        if self._memory.remove_lesson(arg):
            self._send_text_nowait(platform, chat_id, f"✅ Forgot lessons matching: {arg}")
        else:
            self._send_text_nowait(platform, chat_id, f"❌ No lessons found matching: {arg}")

    def _handle_memory_command(self, platform: str, chat_id: str):
        """Handle /memory — show current memory contents."""
        key = self._make_key(platform, chat_id)
        ws_id = self._ctx_builder._workspace_id(platform, chat_id)
        ctx = self._memory.get_context(workspace_id=ws_id)
        if ctx:
            self._send_text_nowait(platform, chat_id, f"🧠 **Current Memory** (workspace: {ws_id})\n\n{ctx}")
        else:
            self._send_text_nowait(platform, chat_id, "🧠 Memory is empty. Use /remember to add knowledge.")

    # ── Project commands ──

    def _handle_project_command(self, platform: str, chat_id: str, key: str, arg: str):
        """Route /project subcommands."""
        if not arg or arg == "ls":
            self._handle_project_list(platform, chat_id, key)
        elif arg == "off":
            self._handle_project_off(platform, chat_id, key)
        elif arg == "close":
            self._handle_project_close(platform, chat_id, key)
        elif arg == "push":
            self._handle_project_push(platform, chat_id, key)
        elif arg.startswith("new "):
            name = arg[4:].strip()
            self._handle_project_new(platform, chat_id, key, name)
        elif arg.isdigit():
            self._handle_project_switch_by_index(platform, chat_id, key, int(arg))
        else:
            self._handle_project_switch(platform, chat_id, key, arg)

    def _build_project_list(self, key: str) -> tuple[list[str], list[str]]:
        """Build ordered lists of active and recent project paths for a chat.

        Returns (active_paths, recent_paths).
        Active = kiro-cli running. Recent = session_map entry but kiro-cli stopped.
        """
        active: list[str] = []
        prefix = f"{key}@"

        with self._acp_lock:
            for acp_key, acp in self._acp_clients.items():
                if acp_key.startswith(prefix) and acp.is_running():
                    project_path = acp_key.split("@", 1)[1]
                    active.append(project_path)

        recent: list[str] = []
        for map_key in list(self._session_map._data.keys()):
            if map_key.startswith(prefix):
                project_path = map_key.split("@", 1)[1]
                if project_path not in active:
                    if self._session_map.get(map_key) is not None:
                        recent.append(project_path)

        return active, recent

    def _handle_project_list(self, platform: str, chat_id: str, key: str):
        """Handle /project ls — list active and recent projects."""
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            current = ctx.active_project if ctx else ""

        active, recent = self._build_project_list(key)
        all_projects = active + recent

        if not all_projects and not current:
            self._send_text_nowait(platform, chat_id,
                                   "📂 No projects loaded.\n\n"
                                   "💡 /project <path> to switch to a project\n"
                                   "💡 /project new <name> to create one")
            return

        lines: list[str] = []
        if current:
            name = os.path.basename(current)
            idx = all_projects.index(current) + 1 if current in all_projects else "?"
            lines.append(f"📂 Current: [{idx}] {name} ◀\n")

        if active:
            lines.append("🟢 **Active** (kiro-cli running):")
            for i, p in enumerate(active, 1):
                name = os.path.basename(p)
                marker = " ◀" if p == current else ""
                lines.append(f"  [{i}] {name} — {p}{marker}")

        if recent:
            lines.append("\n💤 **Recent** (resumable):")
            offset = len(active)
            for i, p in enumerate(recent, offset + 1):
                name = os.path.basename(p)
                lines.append(f"  [{i}] {name} — {p}")

        lines.append("\n💡 /project <number> to switch, /project off to return to main session")
        self._send_text_nowait(platform, chat_id, "\n".join(lines))

    def _resolve_project_path(self, key: str, arg: str) -> str | None:
        """Resolve project path from user input.

        Priority: absolute path → short name match → relative to KIRO_CWD.
        """
        if os.path.isabs(arg):
            path = os.path.realpath(arg)
            return path if os.path.isdir(path) else None

        # Short name: search active + recent projects
        active, recent = self._build_project_list(key)
        for p in active + recent:
            if os.path.basename(p) == arg:
                return p

        # Relative to KIRO_CWD
        base = self._config.kiro.default_cwd or os.getcwd()
        path = os.path.realpath(os.path.join(base, arg))
        return path if os.path.isdir(path) else None

    def _handle_project_switch(self, platform: str, chat_id: str, key: str, arg: str):
        """Handle /project <path or name> — switch to a project."""
        path = self._resolve_project_path(key, arg)
        if not path:
            self._send_text_nowait(platform, chat_id, f"❌ Directory not found: {arg}")
            return

        with self._contexts_lock:
            ctx = self._contexts.get(key)
            if not ctx:
                ctx = ChatContext(chat_id=chat_id, platform=platform)
                self._contexts[key] = ctx
            ctx.active_project = path

        name = os.path.basename(path)
        self._send_text_nowait(platform, chat_id, f"📂 Switched to project: **{name}** ({path})")

    def _handle_project_switch_by_index(self, platform: str, chat_id: str, key: str, idx: int):
        """Handle /project <number> — switch by index from project list."""
        active, recent = self._build_project_list(key)
        all_projects = active + recent
        if idx < 1 or idx > len(all_projects):
            self._send_text_nowait(platform, chat_id,
                                   f"❌ Invalid index: {idx}. Use /project ls to see available projects.")
            return
        path = all_projects[idx - 1]
        self._handle_project_switch(platform, chat_id, key, path)

    def _handle_project_off(self, platform: str, chat_id: str, key: str):
        """Handle /project off — return to main session without destroying project sessions."""
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            if ctx:
                ctx.active_project = ""
        self._send_text_nowait(platform, chat_id, "📂 Returned to main session")

    def _handle_project_close(self, platform: str, chat_id: str, key: str):
        """Handle /project close — destroy current project session and return to main."""
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            active_project = ctx.active_project if ctx else ""

        if not active_project:
            self._send_text_nowait(platform, chat_id, "❌ No active project to close")
            return

        project_key = self._make_project_key(platform, chat_id, active_project)
        self._stop_acp_by_key(project_key)
        self._session_map.delete(project_key)

        with self._contexts_lock:
            ctx = self._contexts.get(key)
            if ctx:
                ctx.active_project = ""

        name = os.path.basename(active_project)
        self._send_text_nowait(platform, chat_id,
                               f"📂 Closed project: **{name}**. Returned to main session.")

    def _handle_project_new(self, platform: str, chat_id: str, key: str, name: str):
        """Handle /project new <name> — create directory, switch, inject init message."""
        if not name:
            self._send_text_nowait(platform, chat_id, "💡 Usage: /project new <name>")
            return

        base = self._config.kiro.default_cwd or os.getcwd()
        path = os.path.realpath(os.path.join(base, name))
        os.makedirs(path, exist_ok=True)

        with self._contexts_lock:
            ctx = self._contexts.get(key)
            if not ctx:
                ctx = ChatContext(chat_id=chat_id, platform=platform)
                self._contexts[key] = ctx
            ctx.active_project = path

        self._send_text_nowait(platform, chat_id, f"📂 Created project: **{name}** ({path})")

        # Inject init message into pending buffer so kiro-cli can help set up
        init_msg = ("This is a new empty project directory. "
                    "Please help me initialize it. "
                    "Ask me what kind of project this is.")
        with self._pending_lock:
            if key not in self._pending_messages:
                self._pending_messages[key] = []
            self._pending_messages[key].append((init_msg, None))
        self._reset_debounce(platform, chat_id, key)

    def _handle_project_push(self, platform: str, chat_id: str, key: str):
        """Handle /project push — inject git push message to kiro-cli."""
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            active_project = ctx.active_project if ctx else ""

        if not active_project:
            self._send_text_nowait(platform, chat_id,
                                   "❌ No active project. Use /project <path> first")
            return

        push_msg = ("Please commit all current changes with an appropriate commit message "
                    "and push to the remote repository. Show me the git status first.")
        with self._pending_lock:
            if key not in self._pending_messages:
                self._pending_messages[key] = []
            self._pending_messages[key].append((push_msg, None))
        self._reset_debounce(platform, chat_id, key)

    def _handle_slash_command(self, platform: str, chat_id: str, cmd: str, args: str) -> str | None:
        """Handle slash command from Discord adapter.
        
        Returns the response text to be sent as interaction followup.
        This is called synchronously from the adapter.
        """
        key = self._make_key(platform, chat_id)
        
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            session_id = ctx.session_id if ctx else None
        
        acp = self._get_acp(platform, chat_id)
        
        if cmd == "help":
            return self._get_help_text()
        
        if cmd == "agent":
            return self._get_agent_response(acp, session_id, args)
        
        if cmd == "model":
            return self._get_model_response(acp, session_id, args)
        
        if cmd == "project":
            # Project commands modify state via _handle_project_command (which uses
            # _send_text_nowait). For slash commands we need a return value instead.
            # Delegate to the text-based handler and capture the response.
            self._handle_project_command(platform, chat_id, key, args)
            return None  # Response already sent by handler
        
        if cmd == "remember":
            if not args:
                return "💡 Usage: /remember <something to remember>"
            self._memory.add_lesson(args)
            return f"✅ Remembered: {args}"
        
        if cmd == "forget":
            if not args:
                return "💡 Usage: /forget <keyword>"
            if self._memory.remove_lesson(args):
                return f"✅ Forgot lessons matching: {args}"
            return f"❌ No lessons found matching: {args}"
        
        if cmd == "memory":
            ws_id = self._ctx_builder._workspace_id(platform, chat_id)
            ctx_text = self._memory.get_context(workspace_id=ws_id)
            if ctx_text:
                return f"🧠 **Current Memory** (workspace: {ws_id})\n\n{ctx_text}"
            return "🧠 Memory is empty. Use /remember to add knowledge."
        
        # Unknown gateway command → try forwarding to kiro-cli
        if session_id and acp:
            try:
                output = acp.execute_command(session_id, f"/{cmd} {args}".strip())
                return output if output else "✓ Done"
            except Exception as e:
                return f"❌ Command failed: {e}"
        
        return f"❓ Unknown command: /{cmd}"
    
    def _get_help_text(self) -> str:
        """Get help text for slash commands."""
        return """📚 **Available Commands:**

**Agent:**
• /agent - List available agents
• /agent agent_name - Switch agent

**Model:**
• /model - List available models
• /model model_name - Switch model

**Project:**
• /project ls - List active and recent projects
• /project <number> - Switch to project by index
• /project <path or name> - Switch to project
• /project new <name> - Create new project
• /project push - Commit and push current project
• /project off - Return to main session
• /project close - Close current project session

**Memory:**
• /remember <text> - Save a preference or rule
• /forget <keyword> - Remove matching memories
• /memory - Show current memory

**Cron:**
• /cron add "name" "message" --every 3600 - Add periodic task
• /cron list - List all cron jobs
• /cron pause <id> - Pause a job
• /cron resume <id> - Resume a job
• /cron remove <id> - Remove a job

**Task:**
• /task run <description> - Decompose and run a multi-step task
• /task status - Show active task progress
• /task cancel - Cancel active task

**Kiro CLI** (forwarded to kiro-cli):
• /compact - Compress context window
• /usage - Show usage and quota
• /tools - List available tools
• /mcp - Show loaded MCP servers
• /clear - Clear conversation

**Other:**
• /cli status - Show all kiro-cli instances and gateway status
• /cli restart - Restart all kiro-cli instances
• /help - Show this help"""
    
    def _get_agent_response(self, acp: ACPClient | None, session_id: str | None, args: str) -> str:
        """Get agent command response."""
        if not session_id:
            return "❌ No session yet. Send a message first."
        
        if not acp:
            return "❌ Kiro is not running"
        
        if not args:
            # List agents
            modes_data = acp.get_session_modes(session_id)
            if not modes_data:
                return "❓ No agent info available"
            
            current_mode = modes_data.get("currentModeId", "")
            available_modes = modes_data.get("availableModes", [])
            
            if not available_modes:
                return "❓ No agents available"
            
            lines = ["📋 **Available agents:**", ""]
            for mode in available_modes:
                mode_id = mode.get("id", "unknown")
                mode_name = mode.get("name", mode_id)
                marker = "▶️" if mode_id == current_mode else "•"
                lines.append(f"{marker} **{mode_name}**")
            
            lines.append("")
            lines.append("💡 Use /agent agent_name to switch")
            return "\n".join(lines)
        else:
            # Switch agent
            valid_ids = set()
            modes_data = acp.get_session_modes(session_id)
            if modes_data:
                for m in modes_data.get("availableModes", []):
                    if m.get("id"):
                        valid_ids.add(m["id"])
                    if m.get("name"):
                        valid_ids.add(m["name"])
            
            if valid_ids and args not in valid_ids:
                return f"❌ Invalid agent: {args}\n\n💡 Use /agent to see available agents"
            
            try:
                acp.session_set_mode(session_id, args)
                # Save mode selection for restoration after session_load
                key = self._session_to_key.get(session_id)
                if key:
                    with self._contexts_lock:
                        ctx = self._contexts.get(key)
                        if ctx:
                            ctx.mode_id = args
                    # Persist to SessionMap so mode survives kiro-cli restart
                    self._session_map.update_mode(key, args)
                return f"✅ Switched to agent: **{args}**"
            except Exception as e:
                return f"❌ Switch failed: {e}"
    
    def _get_model_response(self, acp: ACPClient | None, session_id: str | None, args: str) -> str:
        """Get model command response."""
        if not session_id:
            return "❌ No session yet. Send a message first."
        
        if not acp:
            return "❌ Kiro is not running"
        
        if not args:
            # List models
            options = acp.get_model_options(session_id)
            current_model = acp.get_current_model(session_id)
            
            if not options:
                if current_model:
                    return f"📊 **Current model:** {current_model}\n\n(No other models available)"
                return "❓ No model info available"
            
            lines = ["📋 **Available Models:**", ""]
            for opt in options:
                if isinstance(opt, dict):
                    model_id = opt.get("modelId", "") or opt.get("id", "")
                    model_name = opt.get("name", model_id)
                else:
                    model_id = str(opt)
                    model_name = model_id
                
                if model_id:
                    marker = "▶️" if model_id == current_model else "•"
                    if model_id == model_name:
                        lines.append(f"{marker} {model_id}")
                    else:
                        lines.append(f"{marker} {model_id} - {model_name}")
            
            lines.append("")
            if current_model:
                lines.append(f"**Current:** {current_model}")
            lines.append("💡 Use /model model_name to switch")
            return "\n".join(lines)
        else:
            # Switch model
            options = acp.get_model_options(session_id)
            valid_ids = set()
            if options:
                for opt in options:
                    if isinstance(opt, dict):
                        mid = opt.get("modelId", "") or opt.get("id", "")
                        if mid:
                            valid_ids.add(mid)
                    else:
                        valid_ids.add(str(opt))
            
            if valid_ids and args not in valid_ids:
                return f"❌ Invalid model: {args}\n\n💡 Use /model to see available models"
            
            try:
                acp.session_set_model(session_id, args)
                return f"✅ Switched to model: **{args}**"
            except Exception as e:
                return f"❌ Switch failed: {e}"

    def _reset_debounce(self, platform: str, chat_id: str, key: str):
        """Start or reset the debounce timer for a chat.
        
        Cancels any existing timer and starts a new one. When the timer fires,
        all pending messages are merged and processed as a single turn.
        """
        with self._pending_lock:
            old_timer = self._debounce_timers.get(key)
            if old_timer:
                old_timer.cancel()
            debounce_sec = self._DEBOUNCE_BY_PLATFORM.get(platform, self._DEBOUNCE_DEFAULT)
            timer = threading.Timer(
                debounce_sec,
                self._debounce_fire,
                args=(platform, chat_id, key),
            )
            timer.daemon = True
            self._debounce_timers[key] = timer
            timer.start()

    def _debounce_fire(self, platform: str, chat_id: str, key: str):
        """Called when debounce timer expires. Starts processing in a new thread."""
        with self._pending_lock:
            self._debounce_timers.pop(key, None)
        threading.Thread(
            target=self._process_message,
            args=(platform, chat_id, key),
            daemon=True,
        ).start()

    @staticmethod
    def _merge_messages(messages: list[tuple[str, list | None]]) -> tuple[str, list | None]:
        """Merge multiple pending messages into a single prompt.
        
        Single message is returned as-is. Multiple messages have their text
        joined with newlines and images concatenated.
        """
        if len(messages) == 1:
            return messages[0]

        texts = [text for text, _ in messages if text]
        all_images: list = []
        for _, images in messages:
            if images:
                all_images.extend(images)

        merged_text = "\n".join(texts)
        return merged_text, all_images or None

    def _save_images(self, work_dir: str, images: list[tuple[str, str]]) -> list[str]:
        """Save base64 images to workspace and return absolute file paths."""
        images_dir = os.path.join(work_dir, "images")
        os.makedirs(images_dir, exist_ok=True)

        saved = []
        ts = int(time.time() * 1000)
        ext_map = {
            "image/jpeg": "jpg", "image/png": "png",
            "image/gif": "gif", "image/webp": "webp",
        }

        for i, (b64_data, mime_type) in enumerate(images):
            # Detect real MIME for correct file extension
            detected = ACPClient._detect_image_mime(b64_data)
            if detected:
                mime_type = detected
            ext = ext_map.get(mime_type, "jpg")
            filename = f"{ts}_{i}.{ext}"
            filepath = os.path.join(images_dir, filename)

            with open(filepath, "wb") as f:
                f.write(base64.b64decode(b64_data))

            saved.append(filepath)
            log.info("[Gateway] Saved image: %s (%d bytes)", filepath, os.path.getsize(filepath))

        return saved

    def _cleanup_images(self, platform: str, chat_id: str):
        """Remove saved images for a chat session."""
        work_dir = self._config.get_session_cwd(platform, chat_id)
        images_dir = os.path.join(work_dir, "images")
        if os.path.isdir(images_dir):
            try:
                shutil.rmtree(images_dir)
                log.info("[Gateway] Cleaned up images: %s", images_dir)
            except OSError as e:
                log.warning("[Gateway] Failed to clean images %s: %s", images_dir, e)

    def _process_message(self, platform: str, chat_id: str, key: str):
        """Process pending messages with collect semantics."""
        with self._processing_lock:
            if self._processing.get(key):
                return  # Another thread is already processing; it will drain pending
            self._processing[key] = True

        try:
            self._process_message_loop(platform, chat_id, key)
        finally:
            with self._processing_lock:
                self._processing[key] = False
            # Race condition fix: if new messages arrived while we were finishing,
            # kick off another debounce so they don't get stuck in pending.
            with self._pending_lock:
                if self._pending_messages.get(key):
                    self._reset_debounce(platform, chat_id, key)

    def _process_message_loop(self, platform: str, chat_id: str, key: str):
        """Drain and process pending messages in a loop.
        
        Each iteration merges all currently pending messages into one prompt.
        After processing, checks for new messages that arrived during the run.
        """
        while True:
            with self._pending_lock:
                messages = self._pending_messages.pop(key, [])
            if not messages:
                break

            text, images = self._merge_messages(messages)
            if len(messages) > 1:
                log.info("[Gateway] [%s] Merged %d messages into one prompt", key, len(messages))
            self._process_single_message(platform, chat_id, key, text, images)

    def _process_single_message(self, platform: str, chat_id: str, key: str, text: str, images: list[tuple[str, str]] | None = None):
        """Process a single message."""
        card_handle = None
        adapter = self._adapter_map.get(platform)
        
        # Streaming state
        _stream_lock = threading.Lock()
        _last_stream_update = [0.0]
        _STREAM_INTERVAL = 1.0  # seconds between card updates (Feishu rate limit safe)
        
        def _on_stream(chunk: str, accumulated: str):
            """Called from ACP read thread on each text chunk."""
            # Use _active_cards to get the current card (may change after permission approval)
            current_card = self._active_cards.get(key)
            if not current_card:
                return
            now = time.time()
            with _stream_lock:
                elapsed = now - _last_stream_update[0]
                if elapsed >= _STREAM_INTERVAL:
                    _last_stream_update[0] = now
                else:
                    return
            # Update card outside lock
            try:
                self._update_card(platform, current_card, accumulated + " ▌")
            except Exception as e:
                log.debug("[Gateway] [%s] Stream update error: %s", key, e)
        
        try:
            reply_to = self._reply_targets.pop(key, "")
            card_handle = self._send_card(platform, chat_id, "🤔 Thinking...", reply_to=reply_to)
            # Keep reply_to for platforms where send_card returns None (e.g., Discord)
            if not card_handle and reply_to:
                self._reply_targets[key] = reply_to
            
            # Store card handle for permission UI reuse
            if card_handle:
                self._active_cards[key] = card_handle
            
            # Start typing loop for platforms that don't use card updates (e.g., Discord)
            # Discord's send_card already sends one typing indicator, the loop continues it
            if adapter and not card_handle:
                adapter.start_typing_loop(chat_id)

            try:
                # Determine active project for routing
                with self._contexts_lock:
                    ctx = self._contexts.get(key)
                    active_project = ctx.active_project if ctx else ""
                effective_key = self._make_project_key(platform, chat_id, active_project)

                acp = self._ensure_acp(platform, chat_id, project=active_project)
            except Exception as e:
                log.error("[Gateway] [%s] Failed to start kiro-cli: %s", platform, e)
                error_msg = f"❌ Failed to start Kiro: {e}"
                if card_handle:
                    self._update_card(platform, card_handle, error_msg)
                else:
                    self._send_text(platform, chat_id, error_msg)
                return

            session_id, _is_new = self._get_or_create_session(platform, chat_id, effective_key, acp)
            self._session_to_key[session_id] = key  # Map back to chat key for permissions

            # Save images to workspace
            if images:
                work_dir = active_project or self._config.get_session_cwd(platform, chat_id)
                saved_paths = self._save_images(work_dir, images)
                if saved_paths:
                    path_note = ", ".join(saved_paths)
                    text = (text or "") + f"\n\n[Image saved: {path_note}]"

            # Inject memory context on new sessions (preferences, projects, lessons)
            text = self._ctx_builder.build_message(
                text, is_new_session=_is_new, platform=platform, chat_id=chat_id,
                project=active_project
            )

            # Send to Kiro (with streaming for card-based platforms)
            stream_cb = _on_stream if card_handle else None
            max_retries = 3
            last_error: Exception | None = None
            fallback_used = ""
            for attempt in range(max_retries):
                try:
                    result = acp.session_prompt(session_id, text, images=images, on_stream=stream_cb)
                    break
                except RuntimeError as e:
                    last_error = e
                    error_str = str(e).lower()
                    # Try fallback model on rate limit / capacity errors
                    fallback = self._config.kiro.fallback_model
                    if fallback and not fallback_used and (
                        "rate limit" in error_str or "limit" in error_str
                        or "timeout" in error_str or "capacity" in error_str
                    ):
                        log.info("[Gateway] [%s] Primary model failed, trying fallback: %s",
                                 key, fallback)
                        try:
                            acp.session_set_model(session_id, fallback)
                            result = acp.session_prompt(session_id, text, images=images,
                                                        on_stream=stream_cb)
                            fallback_used = fallback
                            break
                        except Exception:
                            pass  # Fallback also failed, continue retry loop
                    if "ValidationException" in str(e) or "Internal error" in str(e):
                        if attempt < max_retries - 1:
                            log.warning("[Gateway] [%s] Transient error (attempt %d/%d): %s",
                                        platform, attempt + 1, max_retries, e)
                            time.sleep(1)
                            continue
                    raise
            else:
                raise last_error

            # Update activity
            with self._acp_lock:
                self._last_activity[key] = time.time()

            response = format_response(result)
            # Fallback model notice
            if fallback_used:
                response = f"⚡ _{fallback_used}_\n\n{response}"
            # Add project label so user knows which project responded
            if active_project:
                project_name = os.path.basename(active_project)
                response = f"📂 **[{project_name}]**\n\n{response}"
            
            # Context usage warning
            usage_pct = acp.get_context_usage(session_id)
            if usage_pct >= 90:
                response += "\n\n⚠️ Context window **90%** full. Send `/compact` to free space."
            elif usage_pct >= 75:
                response += f"\n\n💡 Context usage: {usage_pct:.0f}%"
            
            # Track message count for memory consolidation
            self._consolidator.on_message(effective_key)
            
            final_card = self._active_cards.get(key) or card_handle
            if final_card:
                self._update_card(platform, final_card, response)
            else:
                final_reply_to = self._reply_targets.pop(key, "")
                self._send_text(platform, chat_id, response, reply_to=final_reply_to)

        except Exception as e:
            log.exception("[Gateway] [%s] Error: %s", platform, e)
            error_msg = str(e)
            if "cancelled" in error_msg.lower():
                error_text = "⏹️ Operation cancelled"
            else:
                error_text = f"❌ Error: {e}"
            
            error_card = self._active_cards.get(key) or card_handle
            if error_card:
                self._update_card(platform, error_card, error_text)
            else:
                self._send_text(platform, chat_id, error_text)
            
            with self._contexts_lock:
                self._contexts.pop(key, None)
            
            # Check if this chat's ACP died
            with self._acp_lock:
                acp = self._acp_clients.get(key)
                if acp is not None and not acp.is_running():
                    log.warning("[Gateway] [%s] kiro-cli died, will restart on next message", key)
                    self._acp_clients.pop(key, None)
                    self._last_activity.pop(key, None)
        
        finally:
            # Clean up active card reference
            self._active_cards.pop(key, None)
            # Always stop typing loop when done
            if adapter and not card_handle:
                adapter.stop_typing_loop(chat_id)

    def _get_or_create_session(self, platform: str, chat_id: str, key: str, acp: ACPClient) -> tuple[str, bool]:
        """Get or create ACP session for a chat.
        
        Returns (session_id, is_new) where is_new=True means a fresh session
        was created (no prior history). Resumed sessions return is_new=False.
        
        Resume flow: SessionMap lookup → session/load → fallback to session/new.
        """
        # Get working directory based on workspace_mode (fixed or per_chat)
        work_dir = self._config.get_session_cwd(platform, chat_id)
        os.makedirs(work_dir, exist_ok=True)

        # 1. In-memory session still alive → reuse
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            if ctx and ctx.session_id:
                log.info("[Gateway] [%s] Reusing session %s", key, ctx.session_id)
                return ctx.session_id, False

        # 2. Try to resume from SessionMap (persisted across kiro-cli restarts)
        saved = self._session_map.get(key)
        if saved:
            resume_sid = saved["sid"]
            saved_mode = saved.get("mode_id", "")
            try:
                acp.session_load(resume_sid, work_dir)
                log.info("[Gateway] [%s] Resumed session %s", key, resume_sid)

                # Restore agent mode if user had switched via /agent
                if saved_mode:
                    try:
                        acp.session_set_mode(resume_sid, saved_mode)
                        log.info("[Gateway] [%s] Restored agent mode: %s", key, saved_mode)
                    except Exception:
                        log.warning("[Gateway] [%s] Failed to restore mode %s", key, saved_mode)

                with self._contexts_lock:
                    self._contexts[key] = ChatContext(
                        chat_id=chat_id,
                        platform=platform,
                        session_id=resume_sid,
                        mode_id=saved_mode,
                    )
                self._session_to_key[resume_sid] = key
                return resume_sid, False

            except Exception as e:
                log.warning("[Gateway] [%s] session/load failed (%s), falling back to session/new", key, e)
                self._session_map.delete(key)

        # 3. Create fresh session
        session_id, modes = acp.session_new(work_dir)
        log.info("[Gateway] [%s] Created session %s (cwd: %s)", key, session_id, work_dir)

        # Set default model
        default_model = self._config.kiro.default_model
        if default_model:
            try:
                acp.session_set_model(session_id, default_model)
                log.info("[Gateway] [%s] Set model: %s", key, default_model)
            except Exception:
                log.debug("[Gateway] [%s] Failed to set model %s", key, default_model)

        with self._contexts_lock:
            self._contexts[key] = ChatContext(
                chat_id=chat_id,
                platform=platform,
                session_id=session_id,
            )
        self._session_to_key[session_id] = key

        # Persist mapping for future resume
        self._session_map.set(key, session_id)
        return session_id, True

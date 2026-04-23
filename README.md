# KiroCLI Bot Gateway

[中文文档](README.zh-CN.md)

Multi-platform bot gateway for Kiro CLI via ACP protocol.

> **Which repo to use?**
> - **This repo** (`kirocli-bot-gateway`): Multi-platform (Feishu + Discord + more). Recommended if you need multiple platforms or plan to expand later.
> - [`feishu-kirocli-bot`](https://github.com/terrificdm/feishu-kirocli-bot): Feishu only, lightweight and simple. Use this if you only need Feishu.

## Supported Platforms

| Platform | Status | Description |
|----------|--------|-------------|
| Feishu (Lark) | ✅ Ready | Group chat (@mention) and private chat |
| Discord | ✅ Ready | Server channels (@mention) and DM |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          Gateway                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │   Feishu    │  │   Discord   │  │   (more)    │   Adapters   │
│  │   Adapter   │  │   Adapter   │  │             │              │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘              │
│         │                │                │                      │
│         └────────────────┼────────────────┘                      │
│                          ▼                                       │
│              ┌───────────────────────┐                           │
│              │   Platform Router     │                           │
│              └───────────┬───────────┘                           │
│                          │                                       │
│         ┌────────────────┼────────────────┐                      │
│         ▼                ▼                ▼                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │  kiro-cli   │  │  kiro-cli   │  │  kiro-cli   │  Per-chat    │
│  │  (chat A)   │  │  (chat B)   │  │  (chat C)   │  instances   │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
└─────────────────────────────────────────────────────────────────┘
```

## Features

- **🔌 Multi-Platform**: Single gateway serves multiple chat platforms
- **🔒 Chat Isolation**: Each chat gets its own Kiro CLI instance for parallel inference
- **📂 Multi-Project**: Switch between projects in the same chat with `/project` commands
- **🧠 Persistent Memory**: User preferences, project context, learned corrections, and daily conversation summaries survive across sessions and platforms — with automatic LLM-driven consolidation
- **🔄 Session Resume**: Conversation history automatically restored after idle timeout, crash, or gateway restart
- **📁 Flexible Workspace Modes**: `per_chat` (user isolation) or `fixed` (shared project)
- **🤖 Multi Feishu Bot**: Run multiple Feishu bots for parallel project work (one bot per project)
- **⏰ Cron Jobs**: Schedule periodic tasks with intervals or cron expressions (`/cron add`)
- **📋 Task Runner**: Decompose complex tasks into steps with parallel execution (`/task run`)
- **💓 Heartbeat**: Periodic background check-in with quiet hours support
- **📏 Context Monitoring**: Automatic context usage warnings (75%/90%) with `/compact` support
- **⚡ Fallback Model**: Auto-switch to backup model on rate limit or capacity errors
- **🔄 Config Hot-Reload**: `.env` changes auto-detected every 30s (no restart needed)
- **💬 Chat Style**: Concise, natural responses — no "Great question!" filler
- **👋 First Session Bootstrap**: Guided introduction on first use, auto-saved to memory
- **🔐 Interactive Permission Approval**: User approves sensitive operations (y/n/t)
- **⚡ On-Demand Startup**: Kiro CLI starts only when needed
- **⏱️ Auto Idle Shutdown**: Configurable idle timeout per chat
- **📊 LRU Eviction**: Automatic cleanup when instance limit is reached
- **🖼️ Image Support**: Send images for visual analysis (JPEG, PNG, GIF, WebP) with auto MIME detection
- **🛑 Cancel Operation**: Send "cancel" to interrupt
- **🔧 MCP & Skills Support**: Global or project-level configuration

## Workspace Modes

This is the most important configuration to understand:

### `per_chat` Mode (Default, Recommended for Multi-User)

```
User A ──→ Session A ──→ /workspace/chat_id_A/
User B ──→ Session B ──→ /workspace/chat_id_B/
User C ──→ Session C ──→ /workspace/chat_id_C/
```

- Each user gets an **isolated subdirectory**
- Users cannot see or modify each other's files
- Kiro CLI loads **global** `~/.kiro/` configuration
- Best for: Public bots, multi-user scenarios

### `fixed` Mode (Recommended for Project Work)

```
User A ──→ Session A ──┐
User B ──→ Session B ──┼──→ /path/to/project/
User C ──→ Session C ──┘
```

- All users share the **same directory**
- Kiro CLI loads **project-level** `.kiro/` configuration
- Best for: Team collaboration on a specific codebase

### MCP & Skills Configuration

| Mode | Config Location | Use Case |
|------|-----------------|----------|
| `per_chat` | `~/.kiro/settings/mcp.json`<br>`~/.kiro/skills/` | Shared tools for all users |
| `fixed` | `{PROJECT}/.kiro/settings/mcp.json`<br>`{PROJECT}/.kiro/skills/` | Project-specific tools |

### Per-Platform Override

Different platforms can use different modes:

```bash
# Global default
KIRO_WORKSPACE_MODE=per_chat

# Override for specific platforms
FEISHU_WORKSPACE_MODE=per_chat   # Public Feishu bot - isolate users
DISCORD_WORKSPACE_MODE=fixed     # Team Discord - shared project
```

## Prerequisites

- Python 3.11+
- [kiro-cli](https://kiro.dev/docs/cli/) installed and logged in (`kiro-cli auth login`)
- Platform-specific bot credentials (see below)

## Installation

```bash
cd kirocli-bot-gateway
pip install -e .
```

## Configuration

```bash
cp .env.example .env
# Edit .env with your configuration
```

See `.env.example` for detailed configuration options and explanations.

## Platform Setup

### Feishu (Lark)

1. Create an enterprise app on [Feishu Open Platform](https://open.feishu.cn/app)
   - Click **Create Enterprise Self-Built App**
   - Fill in app name and description

2. Get credentials: In **Credentials & Basic Info**, copy **App ID** (format: `cli_xxx`) and **App Secret** into your `.env` file

3. Add "Bot" capability: In **App Features** > **Bot**, enable bot — `FEISHU_BOT_NAME` in your `.env` must match the bot's display name in Feishu (usually the same as the app name)

4. Configure permissions (you can bulk import via the Feishu Open Platform permissions page):
   - `im:message` - Read and write messages (base permission)
   - `im:message:send_as_bot` - Send messages as bot
   - `im:message:readonly` - Read message history
   - `im:message.group_at_msg:readonly` - Receive group @messages
   - `im:message.p2p_msg:readonly` - Receive private chat messages
   - `im:chat.access_event.bot_p2p_chat:read` - Private chat events
   - `im:chat.members:bot_access` - Bot group membership access
   - `im:resource` - Access message resources (images, files, etc.)

   <details>
   <summary>Bulk import JSON</summary>

   ```json
   {
     "scopes": {
       "tenant": [
         "im:message",
         "im:message:send_as_bot",
         "im:message:readonly",
         "im:message.group_at_msg:readonly",
         "im:message.p2p_msg:readonly",
         "im:chat.access_event.bot_p2p_chat:read",
         "im:chat.members:bot_access",
         "im:resource"
       ],
       "user": []
     }
   }
   ```

   </details>

5. Start the bot first (required for event subscription to save):
   ```bash
   python main.py
   ```
   The bot only connects to Feishu WebSocket — it won't receive any messages yet, but the connection is needed for the next step.

6. Event subscription: In **Event Subscription**, select **Use long connection to receive events** (WebSocket) — no public webhook URL required
   - Add event: `im.message.receive_v1`

7. Publish the app: In **Version Management & Release**, create a version and publish
   - Enterprise self-built apps are usually auto-approved
   - Permission changes require publishing a new version to take effect

> ⚠️ **External Group Limitation**: Due to Feishu's access control, the bot can **only** be added to internal enterprise groups by default. For external groups, see Feishu documentation.

### Discord

1. Create a Discord application at [Discord Developer Portal](https://discord.com/developers/applications)
   - Click **New Application** and give it a name

2. Create a Bot:
   - Go to **Bot** tab
   - Click **Add Bot** (or it may already exist)
   - Under **Privileged Gateway Intents**, enable:
     - **MESSAGE CONTENT INTENT** (required to read message text)
     - **SERVER MEMBERS INTENT** (recommended for member lookups and allowlist matching)
   - Copy the **Token** into your `.env` as `DISCORD_BOT_TOKEN`

3. Generate invite URL:
   - Go to **OAuth2** > **URL Generator**
   - Select scopes: `bot`, `applications.commands`
   - Select bot permissions:
     - View Channels
     - Send Messages
     - Send Messages in Threads
     - Embed Links
     - Attach Files
     - Read Message History
     - Add Reactions
   - Copy the generated URL and open it to invite the bot to your server

4. Configure `.env`:
   ```bash
   DISCORD_ENABLED=true
   DISCORD_BOT_TOKEN=your_token_here
   DISCORD_GUILD_ID=your_guild_id       # right-click server → Copy ID
   DISCORD_ADMIN_USER_ID=your_user_id   # right-click yourself → Copy ID
   DISCORD_REQUIRE_MENTION=true          # whether @mention is required
   DISCORD_SLASH_COMMANDS=true           # enable /help, /agent, /model
   ```

   > **That's it for most users!** The bot will allow DMs from you and respond in your server.
   > No extra config files needed.

5. **Advanced: Fine-grained access control** (optional):
   
   For per-guild, per-channel, per-user control, create `discord_policy.json`:
   ```bash
   cp discord_policy.example.json discord_policy.json
   # Edit discord_policy.json with your IDs
   ```

   When `discord_policy.json` exists, it **overrides** the env var settings above.

   Example policy:
   ```json
   {
     "dm": {
       "enabled": true,
       "policy": "allowlist",
       "allowFrom": ["YOUR_USER_ID"]
     },
     "groupPolicy": "allowlist",
     "guilds": {
       "*": {
         "requireMention": true
       },
       "YOUR_GUILD_ID": {
         "requireMention": false,
         "users": ["YOUR_USER_ID"],
         "channels": {
           "*": { "allow": true },
           "CHANNEL_ID": {
             "allow": true,
             "requireMention": true,
             "users": ["USER_ID_1", "USER_ID_2"]
           }
         }
       }
     },
     "allowBots": false
   }
   ```

   **Policy options:**
   - `dm.enabled`: Enable/disable DM (default: true)
   - `dm.policy`: `"allowlist"` (only listed users) | `"open"` (anyone) | `"disabled"`
   - `dm.allowFrom`: List of user IDs allowed to DM
   - `groupPolicy`: `"allowlist"` (only listed guilds/channels) | `"open"` | `"disabled"`
   - `guilds.<id>.users`: Per-guild user allowlist (empty = anyone)
   - `guilds.<id>.channels.<id>.allow`: Allow specific channels
   - `guilds.<id>.channels.<id>.requireMention`: Per-channel mention override
   - `guilds.<id>.channels.<id>.users`: Per-channel user allowlist
   - `guilds.<id>.requireMention`: Whether @mention is required (default: true)
   - `guilds."*"`: Default settings for unlisted guilds
   - `allowBots`: Whether to respond to other bots (default: false)

   **How to get IDs:**
   - Enable Developer Mode: Discord Settings → Advanced → Developer Mode
   - Right-click user/server/channel → Copy ID

   **Access control priority:**
   1. `discord_policy.json` (if exists) — full control
   2. `DISCORD_ADMIN_USER_ID` (if set) — simple allowlist
   3. Neither — DM disabled, guilds open with @mention required

6. Start the gateway:
   ```bash
   python main.py
   ```

**Usage:**
- **In servers**: @mention the bot to interact (unless `requireMention: false`)
- **In DMs**: Send messages directly (if allowed by policy)

## Running

```bash
python main.py
```

### Running as a systemd service (optional)

For auto-restart and boot autostart:

```bash
# Copy and edit the service file: update paths for your environment
cp kiro-gateway.service.example kiro-gateway.service
# Edit kiro-gateway.service with your actual paths
sudo cp kiro-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kiro-gateway
sudo systemctl start kiro-gateway

# Check status / logs
sudo systemctl status kiro-gateway
journalctl -u kiro-gateway -f
```

> **⚠️ Note:** systemd does not inherit your shell's PATH. If kiro-cli or MCP servers
> (e.g. npx-based) fail with "No such file or directory", edit the `Environment=PATH=...`
> line in `kiro-gateway.service` to include the paths where `kiro-cli`, `npx`, etc.
> are installed (e.g. `~/.local/bin`, nvm's `bin` directory).

## Usage

### Image Support

Send images alongside text for Kiro to analyze — screenshots, diagrams, error messages, etc.

- **Supported formats**: JPEG, PNG, GIF, WebP
- **Auto MIME detection**: The gateway detects the actual image format from file data, correcting any misreported MIME types from platforms
- **Image persistence**: Images are saved to workspace so Kiro can re-read them in subsequent turns

### Chat Commands

| Platform | Trigger |
|----------|---------|
| Feishu Group | @bot + message |
| Feishu Private | Direct message |
| Discord Server | @bot + message |
| Discord DM | Direct message |

### Slash Commands

All commands work on both platforms, but the input method differs:

| Platform | How to use commands |
|----------|-------------------|
| **Discord** | Native slash commands with autocomplete — type `/` to see suggestions |
| **Feishu** | Type as regular text messages (e.g. send `/project ls` as a message) |

| Command | Description |
|---------|-------------|
| `/agent` | List available agents |
| `/agent <name>` | Switch to agent |
| `/model` | List available models |
| `/model <name>` | Switch to model |
| `/project ls` | List active and recent projects (numbered) |
| `/project <number>` | Switch to project by index |
| `/project <path or name>` | Switch to project by path or short name |
| `/project new <name>` | Create new project directory |
| `/project push` | Commit and push current project (via Kiro) |
| `/project off` | Return to main session |
| `/project close` | Close current project session permanently |
| `/remember <text>` | Save a preference or rule to persistent memory |
| `/forget <keyword>` | Remove matching memories |
| `/memory` | Show current memory contents |
| `/help` | Show help |

**Cron** (periodic background tasks):

| Command | Description |
|---------|-------------|
| `/cron add "name" "message" --every 3600` | Schedule a periodic task (interval) |
| `/cron add "name" "message" --schedule "0 9 * * 1-5"` | Schedule with cron expression |
| `/cron list` | List all cron jobs |
| `/cron pause <id>` | Pause a job |
| `/cron resume <id>` | Resume a paused job |
| `/cron remove <id>` | Remove a job |

**Task** (multi-step task execution):

| Command | Description |
|---------|-------------|
| `/task run <description>` | Decompose task into steps, show plan, then execute |
| `/task status` | Show active task progress |
| `/task cancel` | Cancel active task |

After `/task run`, the bot shows a plan with parallel groups. Reply **go** to start execution.

**Kiro CLI commands** (forwarded to kiro-cli for execution):

| Command | Description |
|---------|-------------|
| `/compact` | Compress context window (free up space for longer conversations) |
| `/usage` | Show API usage and quota |
| `/tools` | List available tools and permissions |
| `/mcp` | Show loaded MCP servers |
| `/clear` | Clear conversation history |

Any unrecognized `/command` is automatically forwarded to kiro-cli. Context usage is monitored — you'll see a warning when the context window reaches 75% or 90%.

**Gateway management:**

| Command | Description |
|---------|-------------|
| `/cli status` | Show all kiro-cli instances, context usage, cron jobs, and active task |
| `/cli restart` | Stop all kiro-cli instances and restart background (sessions resume on next message) |

### Other Commands

| Command | Description |
|---------|-------------|
| `cancel` / `stop` | Cancel current operation |

### Permission Approval

When Kiro needs to perform sensitive operations:

```
🔐 Kiro requests permission:
📋 Creating file: hello.txt
Reply: y(allow) / n(deny) / t(trust)
⏱️ Auto-deny in 60s
```

- **y** / yes / ok - Allow once
- **n** / no - Deny
- **t** / trust / always - Always allow this operation type

## Icon Legend

| Icon | Meaning |
|------|---------|
| 📄 | File read |
| 📝 | File edit |
| ⚡ | Terminal command |
| 🔧 | Other tool |
| ✅ | Success |
| ❌ | Failed |
| ⏳ | In progress |
| 🚫 | Rejected |
| 🔐 | Permission request |

## Project Structure

```
kirocli-bot-gateway/
├── main.py                        # Entry point
├── gateway.py                     # Core gateway logic
├── config.py                      # Configuration management
├── acp_client.py                  # ACP protocol client
├── session_map.py                 # Session resume: chat_key → kiro-cli session ID
├── memory.py                      # Two-layer persistent memory (prefs/lessons/history + projects)
├── context.py                     # Context builder: injects memory into new sessions
├── consolidator.py                # LLM-driven memory extraction from conversations
├── cron.py                        # Cron service: periodic task scheduling
├── task_runner.py                 # Task runner: multi-step decomposition and parallel execution
├── .env.example                   # Environment config template (copy to .env)
├── feishu_bots.example.json       # Multi Feishu bot config template (optional)
├── discord_policy.json            # Discord access policy (optional, overrides env vars)
├── discord_policy.example.json    # Example Discord policy (copy and edit)
├── pyproject.toml                 # Python package config
├── kiro-gateway.service.example   # systemd service template (copy and edit)
└── adapters/
    ├── __init__.py                # Package exports
    ├── base.py                    # ChatAdapter interface
    ├── feishu.py                  # Feishu implementation
    └── discord.py                 # Discord implementation
```

### Persistent State (`~/.kirocli-gateway/`)

```
~/.kirocli-gateway/
├── session_map.json               # Chat key → kiro-cli session ID mapping
├── crons.json                     # Scheduled periodic tasks
└── memory/
    ├── preferences.md             # Global user preferences (backed up as .bak)
    ├── preferences.md.bak         # Previous version (auto-rotated on write)
    ├── lessons.md                 # Global learned corrections (backed up as .bak)
    ├── lessons.md.bak             # Previous version
    ├── history/                   # Daily conversation summaries (3-level decay)
    │   ├── 2026-04-21.md          # Today: full content
    │   ├── 2026-04-20.md          # Yesterday: full content
    │   └── ...                    # 4-30d: summarized, 31-90d: count only, 90d+: deleted
    └── workspaces/
        ├── _global/projects.md    # Project context (per_chat mode)
        └── {hash}/projects.md     # Project context (fixed mode, per project dir)
```

**Memory consolidation**: After 15+ messages and 60s of idle time, the gateway automatically analyzes the conversation using kiro-cli's LLM to extract preferences, project context, lessons, and a daily summary — no manual `/remember` needed. Memory files are backed up (`.bak`) before each LLM-driven update.

## Multi-Project Sessions

Work on multiple projects in the same chat. Each project gets its own Kiro CLI instance with independent conversation history and project-level `.kiro/` configuration.

```
/project /projects/myapp     → Switch to myapp (starts kiro-cli with cwd=/projects/myapp)
"Fix the login bug"          → Routed to myapp's kiro-cli
/project /projects/infra     → Switch to infra (myapp keeps running)
"Check CDK config"           → Routed to infra's kiro-cli
/project 1                   → Switch back to myapp by index
"Is the bug fixed?"          → myapp's kiro-cli has full conversation history
/project off                 → Return to main session
```

- `/project off` keeps project sessions alive (instant switch back)
- `/project close` destroys the current project session permanently
- `/project push` asks Kiro to commit and push (with tool approval)
- `/project new <name>` creates a directory and initializes via Kiro
- Idle project sessions are auto-reclaimed and restored via `session/load` when needed

## Multiple Feishu Bots

Feishu limits each bot to one private chat per user. To work on multiple projects **in parallel**, create multiple Feishu bots — each gets its own chat window.

1. Create multiple enterprise apps on [Feishu Open Platform](https://open.feishu.cn/app) (one per project)
2. Create `feishu_bots.json` from the template:
   ```bash
   cp feishu_bots.example.json feishu_bots.json
   ```
3. Configure each bot:
   ```json
   {
     "bots": [
       {
         "name": "app",
         "app_id": "cli_xxx1",
         "app_secret": "secret1",
         "bot_name": "Kiro-App",
         "kiro_cwd": "/projects/myapp",
         "workspace_mode": "fixed"
       },
       {
         "name": "infra",
         "app_id": "cli_xxx2",
         "app_secret": "secret2",
         "bot_name": "Kiro-Infra",
         "kiro_cwd": "/projects/infra",
         "workspace_mode": "fixed"
       }
     ]
   }
   ```
4. Start the gateway — all bots connect simultaneously:
   ```bash
   python main.py
   ```

When `feishu_bots.json` exists, it overrides the single-bot `FEISHU_APP_ID`/`FEISHU_APP_SECRET` in `.env`. Without the file, single-bot mode works as before (fully backward compatible).

## Adding New Platforms

1. Create `adapters/yourplatform.py`
2. Implement `ChatAdapter` interface from `adapters/base.py`
3. Add configuration in `config.py`
4. Register adapter in `main.py`

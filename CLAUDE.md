# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Agent Info

- **Model**: `opus[1m]` (Claude Opus, 1M context window) — set in `~/.claude/settings.json`
- **Effort level**: `medium`
- **Agent teams**: enabled (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`)

## Project Overview

Slack bot providing remote access to Claude Code. Python 3.11+, built with Poetry, using `slack-bolt` for Slack and `claude-agent-sdk` for Claude Code integration. Uses Socket Mode for WebSocket-based communication (no public URL needed).

## Commands

```bash
make dev              # Install all deps (including dev)
make install          # Production deps only
make run              # Run the bot
make run-debug        # Run with debug logging
make test             # Run tests with coverage
make lint             # Black + isort + flake8 + mypy
make format           # Auto-format with black + isort

# Run a single test
poetry run pytest tests/unit/test_config.py -k test_name -v

# Type checking only
poetry run mypy src
```

## Architecture

### Claude SDK Integration

`ClaudeIntegration` (facade in `src/claude/facade.py`) wraps `ClaudeSDKManager` (`src/claude/sdk_integration.py`), which uses `claude-agent-sdk` with `ClaudeSDKClient` for async streaming. Session IDs come from Claude's `ResultMessage`, not generated locally.

Sessions auto-resume: per user+directory, persisted in SQLite.

### Request Flow

**Agentic mode** (default, `AGENTIC_MODE=true`):

```
Slack message -> Security middleware -> Auth middleware
-> Rate limit middleware -> MessageOrchestrator.agentic_text()
-> ClaudeIntegration.run_command() -> SDK
-> Response parsed -> Stored in SQLite -> Sent back to Slack
```

**External triggers** (webhooks, scheduler):

```
Webhook POST /webhooks/{provider} -> Signature verification -> Deduplication
-> Publish WebhookEvent to EventBus -> AgentHandler.handle_webhook()
-> ClaudeIntegration.run_command() -> Publish AgentResponseEvent
-> NotificationService -> Rate-limited Slack delivery
```

**Classic mode** (`AGENTIC_MODE=false`): Same middleware chain, but routes through full command/message handlers in `src/bot/handlers/` with 13 slash commands and Block Kit buttons.

### Dependency Injection

Bot handlers access dependencies via Bolt's `context` dict:
```python
context["deps"]["auth_manager"]
context["deps"]["claude_integration"]
context["deps"]["storage"]
context["deps"]["security_validator"]
```

### Key Directories

- `src/config/` -- Pydantic Settings v2 config with env detection, feature flags (`features.py`), YAML project loader (`loader.py`)
- `src/bot/handlers/` -- Slack slash command, message event, and action handlers
- `src/bot/middleware/` -- Auth, rate limit, security input validation (Bolt middleware pattern)
- `src/bot/features/` -- Git integration, file handling, quick actions, session export
- `src/bot/orchestrator.py` -- MessageOrchestrator: routes to agentic or classic handlers, project-channel routing
- `src/claude/` -- Claude integration facade, SDK/CLI managers, session management, tool monitoring
- `src/projects/` -- Multi-project support: `registry.py` (YAML project config), `thread_manager.py` (Slack channel sync/routing)
- `src/storage/` -- SQLite via aiosqlite, repository pattern (users, sessions, messages, tool_usage, audit_log, cost_tracking, project_channels)
- `src/security/` -- Multi-provider auth (whitelist + token), input validators, rate limiter, audit logging. User IDs are strings (Slack format: `U01ABC123`).
- `src/events/` -- EventBus (async pub/sub), event types, AgentHandler, EventSecurityMiddleware
- `src/api/` -- FastAPI webhook server, GitHub HMAC-SHA256 + Bearer token auth
- `src/scheduler/` -- APScheduler cron jobs, persistent storage in SQLite
- `src/notifications/` -- NotificationService, rate-limited Slack delivery via WebClient

### Security Model

5-layer defense: authentication (whitelist/token) -> directory isolation (APPROVED_DIRECTORY + path traversal prevention) -> input validation (blocks `..`, `;`, `&&`, `$()`, etc.) -> rate limiting (token bucket) -> audit logging.

`SecurityValidator` blocks access to secrets (`.env`, `.ssh`, `id_rsa`, `.pem`) and dangerous shell patterns. Can be relaxed with `DISABLE_SECURITY_PATTERNS=true` (trusted environments only).

`ToolMonitor` validates Claude's tool calls against allowlist/disallowlist, file path boundaries, and dangerous bash patterns. Tool name validation can be bypassed with `DISABLE_TOOL_VALIDATION=true`.

Webhook authentication: GitHub HMAC-SHA256 signature verification, generic Bearer token for other providers, atomic deduplication via `webhook_events` table.

### Configuration

Settings loaded from environment variables via Pydantic Settings. Required: `SLACK_BOT_TOKEN` (xoxb-...), `SLACK_APP_TOKEN` (xapp-...), `APPROVED_DIRECTORY`. Key optional: `ALLOWED_USERS` (comma-separated Slack user IDs), `ANTHROPIC_API_KEY`, `ENABLE_MCP`, `MCP_CONFIG_PATH`.

Agentic platform settings: `AGENTIC_MODE` (default true), `ENABLE_API_SERVER`, `API_SERVER_PORT` (default 8080), `GITHUB_WEBHOOK_SECRET`, `WEBHOOK_API_SECRET`, `ENABLE_SCHEDULER`, `NOTIFICATION_CHANNEL_IDS`.

Security relaxation (trusted environments only): `DISABLE_SECURITY_PATTERNS` (default false), `DISABLE_TOOL_VALIDATION` (default false).

Multi-project channels: `ENABLE_PROJECT_CHANNELS` (default false), `PROJECTS_CONFIG_PATH` (path to YAML project registry). Each project maps to a dedicated Slack channel (e.g., `#project-myapp`).

Output verbosity: `VERBOSE_LEVEL` (default 1, range 0-2). Controls how much of Claude's background activity is shown to the user. 0 = quiet (only final response), 1 = normal (tool names + reasoning), 2 = detailed (tool inputs + longer reasoning). Users can override per-session via `/verbose 0|1|2`.

Feature flags in `src/config/features.py` control: MCP, git integration, file uploads, quick actions, session export, image uploads, conversation mode, agentic mode, API server, scheduler.

### DateTime Convention

All datetimes use timezone-aware UTC: `datetime.now(UTC)` (not `datetime.utcnow()`). SQLite adapters auto-convert TIMESTAMP/DATETIME columns to `datetime` objects via `detect_types=PARSE_DECLTYPES`. Model `from_row()` methods must guard `fromisoformat()` calls with `isinstance(val, str)` checks.

## Task Tracking Convention

For non-trivial tasks, create a sub-directory under `.claude/tasks/` named after the task (e.g., `support_all_filetypes`). Each task directory contains:

- `plan.md` — Implementation plan written before coding begins
- `tasks.md` — Checklist tracking what's done and what remains

Example: `.claude/tasks/support_all_filetypes/plan.md`

## Git & Deploy Workflow

- **Never push to `main` until the user has confirmed the change works.** Commit locally, restart the bot, and let the user verify in Slack first. Only push after explicit user approval. This applies to both bug fixes and new features.
- The bot runs via `bin/run.sh` which auto-restarts on exit. To deploy changes: `kill $(cat data/bot.pid)` and the wrapper brings it back up in 3 seconds.

## Code Style

- Black (88 char line length), isort (black profile), flake8, mypy strict, autoflake for unused imports
- pytest-asyncio with `asyncio_mode = "auto"`
- structlog for all logging (JSON in prod, console in dev)
- Type hints required on all functions (`disallow_untyped_defs = true`)
- Use `datetime.now(UTC)` not `datetime.utcnow()` (deprecated)
- Message formatting: Slack mrkdwn (`*bold*`, `_italic_`, `` `code` ``, ` ```block``` `)
- UI elements: Block Kit dicts (not Telegram InlineKeyboardMarkup — this is a Slack bot)

## Adding a New Bot Command

### Agentic mode

Agentic mode commands: `/start`, `/new`, `/status`, `/verbose`, `/repo`. If `ENABLE_PROJECT_CHANNELS=true`: `/sync_channels`. To add a new command:

1. Add handler function in `src/bot/orchestrator.py`
2. Register in `MessageOrchestrator._register_agentic_handlers()` using `app.command("/name")`
3. Register the slash command in the Slack App manifest
4. Add audit logging for the command

### Classic mode

1. Add handler function in `src/bot/handlers/command.py`
2. Register in `MessageOrchestrator._register_classic_handlers()` using `app.command("/name")`
3. Register the slash command in the Slack App manifest
4. Add audit logging for the command

# Claude Code Slack Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> Originally derived from [claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram) by [RichardAtCT](https://github.com/RichardAtCT). Refactored for Slack.

A Slack bot that gives you remote access to [Claude Code](https://claude.ai/code). Chat naturally with Claude about your projects from anywhere -- no terminal commands needed.

## What is this?

This bot connects Slack to Claude Code, providing a conversational AI interface for your codebase:

- **Chat naturally** -- ask Claude to analyze, edit, or explain your code in plain language
- **Maintain context** across conversations with automatic session persistence per project
- **Code from anywhere** using Slack on any device
- **Receive proactive notifications** from webhooks, scheduled jobs, and CI/CD events
- **Send and receive files** -- upload code files or images, get generated files back via SlackFileUpload
- **Schedule recurring tasks** -- cron jobs that run Claude prompts and post results to channels
- **Stay secure** with built-in authentication, directory sandboxing, and audit logging

## Quick Start

### 1. Prerequisites

- **Python 3.11+** -- [Download here](https://www.python.org/downloads/)
- **Poetry** -- `pipx install poetry`
- **Claude Code CLI** -- [Install from here](https://claude.ai/code), then `claude auth login`

### 2. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App** > **From scratch**
2. Name it (e.g., "Claude Code") and select your workspace

3. **Enable Socket Mode:**
   - Go to **Socket Mode** in the sidebar, toggle it on
   - Create an **App-Level Token** with scope `connections:write` -- save the `xapp-...` token

4. **Add Bot Scopes:**
   - Go to **OAuth & Permissions** > **Scopes** > **Bot Token Scopes**
   - Add: `chat:write`, `files:read`, `files:write`, `channels:read`, `groups:read`, `im:read`, `im:write`, `im:history`, `channels:history`, `groups:history`, `users:read`

5. **Enable Events:**
   - Go to **Event Subscriptions**, toggle on
   - Subscribe to bot events: `message.channels`, `message.groups`, `message.im`, `file_shared`

6. **Install to Workspace:**
   - Go to **Install App**, click **Install to Workspace**
   - Copy the **Bot User OAuth Token** (`xoxb-...`)

### 3. Install

```bash
git clone https://github.com/iveaves/claude-code-slack.git
cd claude-code-slack
make dev
```

### 4. Configure

```bash
cp .env.example .env
# Edit .env with your settings:
```

**Minimum required:**
```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-level-token
APPROVED_DIRECTORY=/Users/yourname/projects
ALLOWED_USERS=U01ABC123  # Your Slack user ID
```

### 5. Run

```bash
make run          # Production
make run-debug    # With debug logging
```

Message your bot in Slack (DM or in a configured channel) to get started.

> **Detailed setup:** See [docs/setup.md](docs/setup.md) for Claude authentication options and troubleshooting.

## Modes

The bot supports two interaction modes:

### Agentic Mode (Default)

The default conversational mode. Just talk to Claude naturally -- no special commands required.

**Commands:** `/start`, `/new`, `/status`, `/verbose`, `/repo`
If `ENABLE_PROJECT_CHANNELS=true`: `/sync_channels`

```
You: What files are in this project?
Bot: Working... (3s)
     Read
     LS
     Let me describe the project structure
Bot: [Claude describes the project structure]

You: Add a retry decorator to the HTTP client
Bot: Working... (8s)
     Read: http_client.py
     I'll add a retry decorator with exponential backoff
     Edit: http_client.py
     Bash: poetry run pytest tests/ -v
Bot: [Claude shows the changes and test results]

You: /verbose 0
Bot: Verbosity set to 0 (quiet)
```

Use `/verbose 0|1|2` to control how much background activity is shown:

| Level | Shows |
|-------|-------|
| **0** (quiet) | Final response only |
| **1** (normal, default) | Tool names + reasoning snippets in real-time |
| **2** (detailed) | Tool names with inputs + longer reasoning text |

### Classic Mode

Set `AGENTIC_MODE=false` to enable the full 13-command interface with directory navigation, Block Kit buttons, quick actions, git integration, and session export.

**Commands:** `/start`, `/help`, `/new`, `/continue`, `/end`, `/status`, `/cd`, `/ls`, `/pwd`, `/projects`, `/export`, `/actions`, `/git`

## Event-Driven Automation

Beyond direct chat, the bot can respond to external triggers:

- **Webhooks** -- Receive GitHub events (push, PR, issues) and route them through Claude for automated summaries or code review
- **Scheduler** -- Run recurring Claude tasks on a cron schedule (e.g., daily code health checks, creative prompts). Jobs share the channel's session context so you can follow up on results conversationally.
- **Notifications** -- Deliver agent responses to configured Slack channels

Enable with `ENABLE_API_SERVER=true` and `ENABLE_SCHEDULER=true`. See [docs/setup.md](docs/setup.md) for configuration.

## Features

- Conversational agentic mode (default) with natural language interaction
- Classic terminal-like mode with 13 commands and Block Kit buttons
- Full Claude Code integration via SDK with MCP tool support
- CLI subprocess fallback (`USE_SDK=false`)
- Automatic session persistence per user/project directory
- Multi-layer authentication (whitelist + optional token-based)
- Rate limiting with token bucket algorithm
- Directory sandboxing with path traversal prevention
- File upload handling (text files, images, archives)
- Image/screenshot analysis (downloaded and passed to Claude's Read tool)
- SlackFileUpload MCP tool for sending files back to users
- AskUserQuestion MCP tool for interactive Block Kit prompts (SDK mode)
- ScheduleJob/ListScheduledJobs/RemoveScheduledJob MCP tools for cron management
- Git integration with safe repository operations
- Multi-project channel routing (map Slack channels to project directories)
- DM support (map DM channel IDs in projects.yaml)
- Session export in Markdown, HTML, and JSON formats
- SQLite persistence with migrations
- Usage and cost tracking
- Audit logging and security event tracking
- Event bus for decoupled message routing
- Webhook API server (GitHub HMAC-SHA256, generic Bearer token auth)
- Job scheduler with cron expressions and persistent storage
- Notification service with per-channel rate limiting
- Tunable verbose output showing Claude's tool usage and reasoning in real-time
- MCP server support (stdio and HTTP servers via `config/mcp.json`)

## Configuration

### Required

```bash
SLACK_BOT_TOKEN=xoxb-...          # Bot User OAuth Token
SLACK_APP_TOKEN=xapp-...          # App-Level Token (Socket Mode)
APPROVED_DIRECTORY=/path/to/code  # Base directory for project access
ALLOWED_USERS=U01ABC123           # Comma-separated Slack user IDs
```

### Common Options

```bash
# Claude
ANTHROPIC_API_KEY=sk-ant-...     # API key (optional if using CLI auth)
USE_SDK=true                     # SDK (default) or CLI subprocess
CLAUDE_MAX_TURNS=50              # Max conversation turns
CLAUDE_TIMEOUT_SECONDS=300       # Operation timeout
CLAUDE_MAX_COST_PER_USER=10.0   # Spending limit per user (USD)

# Mode
AGENTIC_MODE=true                # Agentic (default) or classic mode
VERBOSE_LEVEL=1                  # 0=quiet, 1=normal (default), 2=detailed

# Security
DISABLE_TOOL_VALIDATION=false    # Skip all tool validation (trusted env only)
DISABLE_SECURITY_PATTERNS=false  # Relax path/command validation (trusted env only)
DEVELOPMENT_MODE=false           # WARNING: allows access outside APPROVED_DIRECTORY

# Rate Limiting
RATE_LIMIT_REQUESTS=10           # Requests per window
RATE_LIMIT_WINDOW=60             # Window in seconds
```

### Agentic Platform

```bash
# Webhook API Server
ENABLE_API_SERVER=false          # Enable FastAPI webhook server
API_SERVER_PORT=8080             # Server port
GITHUB_WEBHOOK_SECRET=...        # GitHub HMAC-SHA256 secret
WEBHOOK_API_SECRET=...           # Bearer token for generic providers

# Scheduler
ENABLE_SCHEDULER=false           # Enable cron job scheduler

# Notifications
NOTIFICATION_CHANNEL_IDS=C01..   # Default Slack channel IDs for proactive notifications
```

### Project Channel Mode

```bash
# Enable strict channel-based project routing
ENABLE_PROJECT_CHANNELS=true

# YAML registry file (see config/projects.example.yaml)
PROJECTS_CONFIG_PATH=config/projects.yaml
```

Map Slack channels to project directories in `projects.yaml`. Supports regular channels and DMs:

```yaml
projects:
  - slug: my-app
    name: My App
    path: my-app          # Relative to APPROVED_DIRECTORY
    enabled: true

  - slug: my-dm
    name: Direct Messages
    path: .
    channel_id: D0AFWB10MU4  # DM channel ID
    enabled: true
```

### MCP Servers

MCP servers are configured in `config/mcp.json`:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "npx",
      "args": ["-y", "my-mcp-server"]
    },
    "remote-server": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": { "API_KEY": "..." }
    }
  }
}
```

Enable with `ENABLE_MCP=true` and `MCP_CONFIG_PATH=config/mcp.json`.

> **Full reference:** See [docs/configuration.md](docs/configuration.md) and [`.env.example`](.env.example).

### Finding Your Slack User ID

In Slack, click on any user's profile picture > **View full profile** > click the **...** menu > **Copy member ID**. User IDs look like `U01ABC123`.

## Troubleshooting

**Bot doesn't respond:**
- Check your `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are correct
- Verify your user ID is in `ALLOWED_USERS`
- Ensure the bot is invited to the channel, or use DMs
- Ensure Claude Code CLI is installed: `claude --version`
- Check bot logs with `make run-debug`

**Claude integration not working:**
- SDK mode (default): Check `claude auth status` or verify `ANTHROPIC_API_KEY`
- CLI mode: Verify `claude --version` and `claude auth status`
- If running from within Claude Code, ensure `CLAUDECODE` env var is unset

**"This channel is not configured for a project":**
- Add the channel to `config/projects.yaml`
- For DMs, use the DM channel ID (starts with `D`)
- Run `/sync_channels` to refresh

**High usage costs:**
- Adjust `CLAUDE_MAX_COST_PER_USER` to set spending limits
- Monitor usage with `/status`
- Lower `CLAUDE_MAX_TURNS` to limit conversation depth

## Security

This bot implements defense-in-depth security:

- **Access Control** -- Whitelist-based user authentication (Slack user IDs)
- **Directory Isolation** -- Sandboxing to `APPROVED_DIRECTORY`
- **Rate Limiting** -- Request and cost-based limits
- **Input Validation** -- Injection and path traversal protection
- **Webhook Authentication** -- GitHub HMAC-SHA256 and Bearer token verification
- **Audit Logging** -- Complete tracking of all user actions

> **WARNING: Development Mode** -- When `DEVELOPMENT_MODE=true`, Claude can access files **outside** `APPROVED_DIRECTORY`. This is logged with warnings but not blocked. Never run development mode in production or on shared machines. Always set `DEVELOPMENT_MODE=false` and `ENVIRONMENT=production` for any deployment beyond local testing.

See [SECURITY.md](SECURITY.md) for details.

## Development

```bash
make dev           # Install all dependencies
make test          # Run tests with coverage
make lint          # Black + isort + flake8 + mypy
make format        # Auto-format code
make run-debug     # Run with debug logging
```

### Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Make changes with tests: `make test && make lint`
4. Submit a Pull Request

**Code standards:** Python 3.11+, Black formatting (88 chars), type hints required, pytest with >85% coverage.

## License

MIT License -- see [LICENSE](LICENSE).

## Acknowledgments

- [Claude](https://claude.ai) by Anthropic
- [slack-bolt](https://github.com/slackapi/bolt-python) by Slack
- [claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram) by RichardAtCT (original Telegram version)

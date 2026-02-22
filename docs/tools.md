# Available Tools

This document describes the tools that Claude Code can use when interacting through the Slack bot. Tools are the operations Claude performs behind the scenes to read, write, search, and execute code on your behalf.

## Overview

By default, the bot allows **20+ tools**. These are configured via the `CLAUDE_ALLOWED_TOOLS` environment variable and validated at runtime by the [ToolMonitor](../src/claude/monitor.py).

When Claude uses a tool during a conversation, the tool name appears in real-time if verbose output is enabled (`/verbose 1` or `/verbose 2`). If Claude attempts to use a tool that is not in the allowed list, the bot blocks the call and displays an error with the list of currently allowed tools.

## Tool Reference

### File Operations

| Tool | Icon | Description |
|------|------|-------------|
| **Read** | 📖 | Read file contents from disk. Supports text files, images, PDFs, and Jupyter notebooks. |
| **Write** | ✏️ | Create a new file or overwrite an existing file with new contents. |
| **Edit** | ✏️ | Perform targeted string replacements within an existing file without rewriting the entire file. |
| **MultiEdit** | ✏️ | Apply multiple edits to a single file in one operation. Useful for making several changes at once. |

### Search & Navigation

| Tool | Icon | Description |
|------|------|-------------|
| **Glob** | 🔍 | Find files by name pattern (e.g., `**/*.py`, `src/**/*.ts`). Returns matching file paths sorted by modification time. |
| **Grep** | 🔍 | Search file contents using regular expressions. Supports filtering by file type or glob pattern, context lines, and multiple output modes. |
| **LS** | 📂 | List directory contents. |

### Execution

| Tool | Icon | Description |
|------|------|-------------|
| **Bash** | 💻 | Execute shell commands (e.g., `git`, `npm`, `pytest`, `make`). Subject to directory boundary enforcement and, in classic mode, dangerous-pattern blocking. |

### Notebooks

| Tool | Icon | Description |
|------|------|-------------|
| **NotebookRead** | 📓 | Read a Jupyter notebook (`.ipynb`) and return all cells with their outputs. |
| **NotebookEdit** | 📓 | Replace, insert, or delete cells in a Jupyter notebook. |

### Web

| Tool | Icon | Description |
|------|------|-------------|
| **WebFetch** | 🌐 | Fetch a URL and process its content. HTML is converted to markdown before analysis. |
| **WebSearch** | 🌐 | Search the web and return results. Useful for looking up documentation, current events, or information beyond Claude's training data. |

### Task Management

| Tool | Icon | Description |
|------|------|-------------|
| **TodoRead** | ☑️ | Read the current task list that Claude uses to track multi-step work. |
| **TodoWrite** | ☑️ | Create or update a task list to plan and track progress on complex operations. |

### Agent Orchestration

| Tool | Icon | Description |
|------|------|-------------|
| **Task** | 🧠 | Launch a sub-agent to handle complex, multi-step operations autonomously. The sub-agent runs with its own context and returns a result when finished. |
| **TaskOutput** | 🧠 | Read the output of a background sub-agent launched by **Task**. Required for retrieving results from agents that were run in the background. |

### Slack Integration (MCP Tools)

These tools are registered as real MCP tools via the SDK and are only available when `USE_SDK=true`. In CLI mode, Claude cannot use these tools.

| Tool | Icon | Description |
|------|------|-------------|
| **SlackFileUpload** | 📎 | Upload a file or image to the current Slack channel. Use this to send generated files, images, or documents to the user. |
| **AskUserQuestion** | ❓ | Present interactive questions to the user via Slack Block Kit. Supports single-select and multi-select options. Only works in SDK mode. |
| **Skill** | ⚡ | Invoke a registered skill (e.g., `/commit`, `/review-pr`). |

### Scheduler Tools (MCP Tools)

These tools manage recurring cron jobs. Only available in SDK mode.

| Tool | Icon | Description |
|------|------|-------------|
| **ScheduleJob** | ⏰ | Schedule a recurring cron job that runs a prompt on a schedule. |
| **ListScheduledJobs** | ⏰ | List all active scheduled jobs. |
| **RemoveScheduledJob** | ⏰ | Remove a scheduled job by its ID. |

## Verbose Output

When verbose output is enabled, each tool call is shown with its icon as Claude works:

```
You: Add type hints to utils.py

Bot: Working... (5s)
     📖 Read: utils.py
     💬 I'll add type annotations to all functions
     ✏️ Edit: utils.py
     💻 Bash: poetry run mypy src/utils.py
Bot: [Claude shows the changes and type-check results]
```

Control verbosity with `/verbose`:

| Level | Behavior |
|-------|----------|
| `/verbose 0` | Final response only (typing indicator stays active) |
| `/verbose 1` | Tool names + reasoning snippets (default) |
| `/verbose 2` | Tool names with input details + longer reasoning text |

## Configuration

### Allowing / Disallowing Tools

The default allowed tools list is defined in `src/config/settings.py` and can be overridden with environment variables:

```bash
# Allow only specific tools (comma-separated)
CLAUDE_ALLOWED_TOOLS=Read,Write,Edit,Bash,Glob,Grep,LS,Task,TaskOutput,MultiEdit,NotebookRead,NotebookEdit,WebFetch,TodoRead,TodoWrite,WebSearch,AskUserQuestion,Skill,ScheduleJob,ListScheduledJobs,RemoveScheduledJob,SlackFileUpload

# Explicitly block specific tools (comma-separated, takes precedence over allowed)
CLAUDE_DISALLOWED_TOOLS=Bash,Write
```

To allow all tools without name-based validation:

```bash
# Skip all tool validation checks (for trusted environments)
DISABLE_TOOL_VALIDATION=true
```

### SDK vs CLI Mode

| Feature | SDK (`USE_SDK=true`) | CLI (`USE_SDK=false`) |
|---------|---------------------|----------------------|
| Core tools (Read, Write, Bash, etc.) | ✅ | ✅ |
| SlackFileUpload | ✅ (MCP tool) | ❌ |
| AskUserQuestion | ✅ (intercepted via `can_use_tool`) | ❌ (auto-dismissed) |
| ScheduleJob / ListScheduledJobs / RemoveScheduledJob | ✅ (MCP tools) | ❌ |
| Cost tracking | ✅ | ❌ (always $0.00) |
| Tool interception | ✅ | ❌ |

### Security Layers

Even when a tool is allowed, additional security checks apply. The exact checks depend on the run mode:

1. **File path validation** (all modes) — `Read`, `Write`, `Edit`, and `MultiEdit` operations must target paths within the `APPROVED_DIRECTORY`. Path traversal attempts are blocked.

2. **Bash command validation** (classic mode only) — Dangerous patterns (`rm -rf`, `sudo`, `chmod 777`, pipes, redirections, subshells) are blocked by default. Filesystem-modifying commands (`mkdir`, `cp`, `mv`, `rm`, etc.) must target paths within the approved directory. This layer is **not active in agentic mode**, which relies on OS-level sandboxing instead.

3. **Bash directory boundary checks** (all modes) — Filesystem-modifying commands are checked to ensure their target paths stay within the approved directory, regardless of run mode.

4. **Audit logging** (all modes) — All tool calls and security violations are recorded for review.

5. **Full bypass** — Set `DISABLE_TOOL_VALIDATION=true` to skip all validation (name checks, path checks, everything). Only for trusted environments.

See [Security](../SECURITY.md) for the full security model.

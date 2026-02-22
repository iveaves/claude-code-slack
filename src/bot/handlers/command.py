"""Command handlers for Slack Bolt bot operations."""

from pathlib import Path
from typing import Optional

import structlog

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ...projects import PrivateTopicsUnavailableError, load_project_registry
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from ..utils.slack_format import escape_mrkdwn

logger = structlog.get_logger()


def _is_within_root(path: Path, root: Path) -> bool:
    """Check whether path is within root directory."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _get_user_state(deps: dict, user_id: str) -> dict:
    """Get per-user state dict from deps, creating if needed."""
    user_states = deps.setdefault("_user_states", {})
    return user_states.setdefault(user_id, {})


def _get_channel_project_root(settings: Settings, user_state: dict) -> Optional[Path]:
    """Get channel project root when strict channel mode is active."""
    if not settings.enable_project_threads:
        return None
    channel_context = user_state.get("_channel_context")
    if not channel_context:
        return None
    return Path(channel_context["project_root"]).resolve()


async def start_command(ack, say, command, client, context) -> None:
    """Handle /start command."""
    await ack()

    user_id = command["user_id"]
    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    audit_logger: AuditLogger = deps.get("audit_logger")
    manager = deps.get("project_threads_manager")
    sync_section = ""

    if settings.enable_project_threads and settings.project_threads_mode == "private":
        # In Slack, "private" mode means DM-only
        channel_info = await client.conversations_info(channel=command["channel_id"])
        is_dm = channel_info["channel"].get("is_im", False)
        if not is_dm:
            await say(
                ":no_entry_sign: *Private Channels Mode*\n\n"
                "Use this bot in a direct message and run `/start` there."
            )
            return

    if (
        settings.enable_project_threads
        and settings.project_threads_mode == "private"
    ):
        if manager is None:
            await say(
                ":x: *Project channel mode is misconfigured*\n\n"
                "Channel manager is not initialized."
            )
            return

        try:
            sync_result = await manager.sync_topics(
                client,
                chat_id=command["channel_id"],
            )
            sync_section = (
                "\n\n:thread: *Project Channels Synced*\n"
                f"- Created: *{sync_result.created}*\n"
                f"- Reused: *{sync_result.reused}*\n"
                f"- Renamed: *{sync_result.renamed}*\n"
                f"- Failed: *{sync_result.failed}*\n\n"
                "Use a project channel to start coding."
            )
        except PrivateTopicsUnavailableError:
            await say(manager.private_topics_unavailable_message())
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="start",
                    args=[],
                    success=False,
                )
            return
        except Exception as e:
            sync_section = (
                "\n\n:warning: *Channel Sync Warning*\n"
                f"{escape_mrkdwn(str(e))}\n\n"
                "Run `/sync_channels` to retry."
            )

    welcome_message = (
        f":wave: Welcome to Claude Code Slack Bot, <@{user_id}>!\n\n"
        f":robot_face: I help you access Claude Code remotely through Slack.\n\n"
        f"*Available Commands:*\n"
        f"- `/help` - Show detailed help\n"
        f"- `/new` - Start a new Claude session\n"
        f"- `/ls` - List files in current directory\n"
        f"- `/cd <dir>` - Change directory\n"
        f"- `/projects` - Show available projects\n"
        f"- `/status` - Show session status\n"
        f"- `/actions` - Show quick actions\n"
        f"- `/git` - Git repository commands\n\n"
        f"*Quick Start:*\n"
        f"1. Use `/projects` to see available projects\n"
        f"2. Use `/cd <project>` to navigate to a project\n"
        f"3. Send any message to start coding with Claude!\n\n"
        f":lock: Your access is secured and all actions are logged.\n"
        f":bar_chart: Use `/status` to check your usage limits."
        f"{sync_section}"
    )

    # Add quick action buttons using Block Kit
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": welcome_message},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":file_folder: Show Projects"},
                    "action_id": "action:show_projects",
                    "value": "show_projects",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":question: Get Help"},
                    "action_id": "action:help",
                    "value": "help",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":new: New Session"},
                    "action_id": "action:new_session",
                    "value": "new_session",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":bar_chart: Check Status"},
                    "action_id": "action:status",
                    "value": "status",
                },
            ],
        },
    ]

    await say(text=welcome_message, blocks=blocks)

    # Log command
    if audit_logger:
        await audit_logger.log_command(
            user_id=user_id, command="start", args=[], success=True
        )


async def help_command(ack, say, command, client, context) -> None:
    """Handle /help command."""
    await ack()

    help_text = (
        ":robot_face: *Claude Code Slack Bot Help*\n\n"
        "*Navigation Commands:*\n"
        "- `/ls` - List files and directories\n"
        "- `/cd <directory>` - Change to directory\n"
        "- `/pwd` - Show current directory\n"
        "- `/projects` - Show available projects\n\n"
        "*Session Commands:*\n"
        "- `/new` - Clear context and start a fresh session\n"
        "- `/continue [message]` - Explicitly continue last session\n"
        "- `/end` - End current session and clear context\n"
        "- `/status` - Show session and usage status\n"
        "- `/export` - Export session history\n"
        "- `/actions` - Show context-aware quick actions\n"
        "- `/git` - Git repository information\n\n"
        "*Session Behavior:*\n"
        "- Sessions are automatically maintained per project directory\n"
        "- Switching directories with `/cd` resumes the session for that project\n"
        "- Use `/new` or `/end` to explicitly clear session context\n"
        "- Sessions persist across bot restarts\n\n"
        "*Usage Examples:*\n"
        "- `cd myproject` - Enter project directory\n"
        "- `ls` - See what's in current directory\n"
        "- `Create a simple Python script` - Ask Claude to code\n"
        "- Send a file to have Claude review it\n\n"
        "*File Operations:*\n"
        "- Send text files (.py, .js, .md, etc.) for review\n"
        "- Claude can read, modify, and create files\n"
        "- All file operations are within your approved directory\n\n"
        "*Security Features:*\n"
        "- :lock: Path traversal protection\n"
        "- :stopwatch: Rate limiting to prevent abuse\n"
        "- :bar_chart: Usage tracking and limits\n"
        "- :shield: Input validation and sanitization\n\n"
        "*Tips:*\n"
        "- Use specific, clear requests for best results\n"
        "- Check `/status` to monitor your usage\n"
        "- Use quick action buttons when available\n"
        "- File uploads are automatically processed by Claude\n\n"
        "Need more help? Contact your administrator."
    )

    await say(help_text)


async def sync_channels(ack, say, command, client, context) -> None:
    """Synchronize project channels in the workspace."""
    await ack()

    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    audit_logger: AuditLogger = deps.get("audit_logger")
    user_id = command["user_id"]

    if not settings.enable_project_threads:
        await say(":information_source: *Project channel mode is disabled.*")
        return

    manager = deps.get("project_threads_manager")
    if not manager:
        await say(":x: *Project channel manager not initialized.*")
        return

    status_msg = await say(":arrows_counterclockwise: *Syncing project channels...*")
    status_ts = status_msg["ts"]
    channel_id = command["channel_id"]

    if settings.project_threads_mode == "private":
        channel_info = await client.conversations_info(channel=channel_id)
        is_dm = channel_info["channel"].get("is_im", False)
        if not is_dm:
            await client.chat_update(
                channel=channel_id,
                ts=status_ts,
                text=(
                    ":x: *Private Channel Mode*\n\n"
                    "Run `/sync_channels` in your direct message with the bot."
                ),
            )
            return
        target_chat_id = channel_id
    else:
        if settings.project_threads_chat_id is None:
            await client.chat_update(
                channel=channel_id,
                ts=status_ts,
                text=(
                    ":x: *Group Channel Mode Misconfigured*\n\n"
                    "Set `PROJECT_THREADS_CHAT_ID` first."
                ),
            )
            return
        if channel_id != settings.project_threads_chat_id:
            await client.chat_update(
                channel=channel_id,
                ts=status_ts,
                text=(
                    ":x: *Group Channel Mode*\n\n"
                    "Run `/sync_channels` in the configured project channels group."
                ),
            )
            return
        target_chat_id = settings.project_threads_chat_id

    try:
        if not settings.projects_config_path:
            await client.chat_update(
                channel=channel_id,
                ts=status_ts,
                text=(
                    ":x: *Project channel mode is misconfigured*\n\n"
                    "Set `PROJECTS_CONFIG_PATH` to a valid YAML file."
                ),
            )
            if audit_logger:
                await audit_logger.log_command(user_id, "sync_channels", [], False)
            return

        registry = load_project_registry(
            config_path=settings.projects_config_path,
            approved_directory=settings.approved_directory,
        )
        manager.registry = registry
        deps["project_registry"] = registry

        result = await manager.sync_topics(client, chat_id=target_chat_id)
        await client.chat_update(
            channel=channel_id,
            ts=status_ts,
            text=(
                ":white_check_mark: *Project channel sync complete*\n\n"
                f"- Created: *{result.created}*\n"
                f"- Reused: *{result.reused}*\n"
                f"- Renamed: *{result.renamed}*\n"
                f"- Reopened: *{result.reopened}*\n"
                f"- Closed: *{result.closed}*\n"
                f"- Deactivated: *{result.deactivated}*\n"
                f"- Failed: *{result.failed}*"
            ),
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_channels", [], True)
    except PrivateTopicsUnavailableError:
        await client.chat_update(
            channel=channel_id,
            ts=status_ts,
            text=manager.private_topics_unavailable_message(),
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_channels", [], False)
    except Exception as e:
        await client.chat_update(
            channel=channel_id,
            ts=status_ts,
            text=f":x: *Project channel sync failed*\n\n{escape_mrkdwn(str(e))}",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_channels", [], False)


async def new_session(ack, say, command, client, context) -> None:
    """Handle /new command - explicitly starts a fresh session, clearing previous context."""
    await ack()

    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    user_id = command["user_id"]
    user_state = _get_user_state(deps, user_id)

    # Get current directory (default to approved directory)
    current_dir = user_state.get("current_directory", settings.approved_directory)
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Track what was cleared for user feedback
    old_session_id = user_state.get("claude_session_id")

    # Clear existing session data
    user_state["claude_session_id"] = None
    user_state["session_started"] = True
    user_state["force_new_session"] = True

    cleared_info = ""
    if old_session_id:
        cleared_info = (
            f"\n:wastebasket: Previous session `{old_session_id[:8]}...` cleared."
        )

    text = (
        f":new: *New Claude Code Session*\n\n"
        f":file_folder: Working directory: `{relative_path}/`{cleared_info}\n\n"
        f"Context has been cleared. Send a message to start fresh, "
        f"or use the buttons below:"
    )

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":memo: Start Coding"},
                    "action_id": "action:start_coding",
                    "value": "start_coding",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":file_folder: Change Project"},
                    "action_id": "action:show_projects",
                    "value": "show_projects",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":clipboard: Quick Actions"},
                    "action_id": "action:quick_actions",
                    "value": "quick_actions",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":question: Help"},
                    "action_id": "action:help",
                    "value": "help",
                },
            ],
        },
    ]

    await say(text=text, blocks=blocks)


async def continue_session(ack, say, command, client, context) -> None:
    """Handle /continue command with optional prompt."""
    await ack()

    user_id = command["user_id"]
    channel_id = command["channel_id"]
    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    claude_integration: ClaudeIntegration = deps.get("claude_integration")
    audit_logger: AuditLogger = deps.get("audit_logger")
    user_state = _get_user_state(deps, user_id)

    # Parse optional prompt from command text
    prompt = command.get("text", "").strip() or None
    default_prompt = "Please continue where we left off"

    current_dir = user_state.get("current_directory", settings.approved_directory)

    try:
        if not claude_integration:
            await say(
                ":x: *Claude Integration Not Available*\n\n"
                "Claude integration is not properly configured."
            )
            return

        # Check if there's an existing session in user state
        claude_session_id = user_state.get("claude_session_id")

        if claude_session_id:
            status_msg = await say(
                f":arrows_counterclockwise: *Continuing Session*\n\n"
                f"Session ID: `{claude_session_id[:8]}...`\n"
                f"Directory: `{current_dir.relative_to(settings.approved_directory)}/`\n\n"
                f"{'Processing your message...' if prompt else 'Continuing where you left off...'}"
            )

            claude_response = await claude_integration.run_command(
                prompt=prompt or default_prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=claude_session_id,
            )
        else:
            status_msg = await say(
                ":mag: *Looking for Recent Session*\n\n"
                "Searching for your most recent session in this directory..."
            )

            claude_response = await claude_integration.continue_session(
                user_id=user_id,
                working_directory=current_dir,
                prompt=prompt or default_prompt,
            )

        if claude_response:
            # Update session ID in state
            user_state["claude_session_id"] = claude_response.session_id

            # Delete status message and send response
            await client.chat_delete(channel=channel_id, ts=status_msg["ts"])

            # Format and send Claude's response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            for msg in formatted_messages:
                await say(msg.text)

            # Log successful continue
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="continue",
                    args=[command.get("text", "")],
                    success=True,
                )

        else:
            # No session found to continue
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":x: *No Session Found*\n\n"
                            f"No recent Claude session found in this directory.\n"
                            f"Directory: `{current_dir.relative_to(settings.approved_directory)}/`\n\n"
                            f"*What you can do:*\n"
                            f"- Use `/new` to start a fresh session\n"
                            f"- Use `/status` to check your sessions\n"
                            f"- Navigate to a different directory with `/cd`"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": ":new: New Session"},
                            "action_id": "action:new_session",
                            "value": "new_session",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": ":bar_chart: Status"},
                            "action_id": "action:status",
                            "value": "status",
                        },
                    ],
                },
            ]
            await client.chat_update(
                channel=channel_id,
                ts=status_msg["ts"],
                text=":x: No session found",
                blocks=blocks,
            )

    except Exception as e:
        error_msg = str(e)
        logger.error("Error in continue command", error=error_msg, user_id=user_id)

        # Delete status message if it exists
        try:
            if "status_msg" in locals():
                await client.chat_delete(channel=channel_id, ts=status_msg["ts"])
        except Exception:
            pass

        # Send error response
        await say(
            f":x: *Error Continuing Session*\n\n"
            f"An error occurred while trying to continue your session:\n\n"
            f"`{error_msg}`\n\n"
            f"*Suggestions:*\n"
            f"- Try starting a new session with `/new`\n"
            f"- Check your session status with `/status`\n"
            f"- Contact support if the issue persists"
        )

        # Log failed continue
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="continue",
                args=[command.get("text", "")],
                success=False,
            )


async def list_files(ack, say, command, client, context) -> None:
    """Handle /ls command."""
    await ack()

    user_id = command["user_id"]
    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    audit_logger: AuditLogger = deps.get("audit_logger")
    user_state = _get_user_state(deps, user_id)

    # Get current directory
    current_dir = user_state.get("current_directory", settings.approved_directory)

    try:
        # List directory contents
        directories = []
        files = []

        for item in sorted(current_dir.iterdir()):
            # Skip hidden files (starting with .)
            if item.name.startswith("."):
                continue

            safe_name = escape_mrkdwn(item.name)

            if item.is_dir():
                directories.append(f":file_folder: {safe_name}/")
            else:
                try:
                    size = item.stat().st_size
                    size_str = _format_file_size(size)
                    files.append(f":page_facing_up: {safe_name} ({size_str})")
                except OSError:
                    files.append(f":page_facing_up: {safe_name}")

        # Combine directories first, then files
        items = directories + files

        # Format response
        relative_path = current_dir.relative_to(settings.approved_directory)
        if not items:
            message = f":open_file_folder: `{relative_path}/`\n\n_(empty directory)_"
        else:
            message = f":open_file_folder: `{relative_path}/`\n\n"

            # Limit items shown to prevent message being too long
            max_items = 50
            if len(items) > max_items:
                shown_items = items[:max_items]
                message += "\n".join(shown_items)
                message += f"\n\n_... and {len(items) - max_items} more items_"
            else:
                message += "\n".join(items)

        # Add navigation buttons if not at root
        elements = []
        if current_dir != settings.approved_directory:
            elements.extend([
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":arrow_up: Go Up"},
                    "action_id": "cd:..",
                    "value": "..",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":house: Go to Root"},
                    "action_id": "cd:/",
                    "value": "/",
                },
            ])

        elements.extend([
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":arrows_counterclockwise: Refresh"},
                "action_id": "action:refresh_ls",
                "value": "refresh_ls",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":file_folder: Projects"},
                "action_id": "action:show_projects",
                "value": "show_projects",
            },
        ])

        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": message}},
            {"type": "actions", "elements": elements},
        ]

        await say(text=message, blocks=blocks)

        # Log successful command
        if audit_logger:
            await audit_logger.log_command(user_id, "ls", [], True)

    except Exception as e:
        error_msg = f":x: Error listing directory: {str(e)}"
        await say(error_msg)

        # Log failed command
        if audit_logger:
            await audit_logger.log_command(user_id, "ls", [], False)

        logger.error("Error in list_files command", error=str(e), user_id=user_id)


async def change_directory(ack, say, command, client, context) -> None:
    """Handle /cd command."""
    await ack()

    user_id = command["user_id"]
    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    security_validator: SecurityValidator = deps.get("security_validator")
    audit_logger: AuditLogger = deps.get("audit_logger")
    user_state = _get_user_state(deps, user_id)

    # Parse arguments
    target_path = command.get("text", "").strip()
    if not target_path:
        await say(
            "*Usage:* `/cd <directory>`\n\n"
            "*Examples:*\n"
            "- `/cd myproject` - Enter subdirectory\n"
            "- `/cd ..` - Go up one level\n"
            "- `/cd /` - Go to root of approved directory\n\n"
            "*Tips:*\n"
            "- Use `/ls` to see available directories\n"
            "- Use `/projects` to see all projects"
        )
        return

    current_dir = user_state.get("current_directory", settings.approved_directory)
    project_root = _get_channel_project_root(settings, user_state)
    directory_root = project_root or settings.approved_directory

    try:
        # Handle known navigation shortcuts first
        if target_path == "/":
            resolved_path = directory_root
        elif target_path == "..":
            resolved_path = current_dir.parent
            if not _is_within_root(resolved_path, directory_root):
                resolved_path = directory_root
        else:
            # Validate path using security validator
            if security_validator:
                valid, resolved_path, error = security_validator.validate_path(
                    target_path, current_dir
                )

                if not valid:
                    await say(f":x: *Access Denied*\n\n{error}")

                    # Log security violation
                    if audit_logger:
                        await audit_logger.log_security_violation(
                            user_id=user_id,
                            violation_type="path_traversal_attempt",
                            details=f"Attempted path: {target_path}",
                            severity="medium",
                        )
                    return
            else:
                resolved_path = current_dir / target_path
                resolved_path = resolved_path.resolve()

        if project_root and not _is_within_root(resolved_path, project_root):
            await say(
                ":x: *Access Denied*\n\n"
                "In channel mode, navigation is limited to the current project root."
            )
            return

        # Check if directory exists and is actually a directory
        if not resolved_path.exists():
            await say(
                f":x: *Directory Not Found*\n\n`{target_path}` does not exist."
            )
            return

        if not resolved_path.is_dir():
            await say(
                f":x: *Not a Directory*\n\n`{target_path}` is not a directory."
            )
            return

        # Update current directory in user state
        user_state["current_directory"] = resolved_path

        # Look up existing session for the new directory instead of clearing
        claude_integration: ClaudeIntegration = deps.get("claude_integration")
        resumed_session_info = ""
        if claude_integration:
            existing_session = await claude_integration._find_resumable_session(
                user_id, resolved_path
            )
            if existing_session:
                user_state["claude_session_id"] = existing_session.session_id
                resumed_session_info = (
                    f"\n:arrows_counterclockwise: Resumed session `{existing_session.session_id[:8]}...` "
                    f"({existing_session.message_count} messages)"
                )
            else:
                # No session for this directory - clear the current one
                user_state["claude_session_id"] = None
                resumed_session_info = (
                    "\n:new: No existing session. Send a message to start a new one."
                )

        # Send confirmation
        relative_base = project_root or settings.approved_directory
        relative_path = resolved_path.relative_to(relative_base)
        relative_display = "/" if str(relative_path) == "." else f"{relative_path}/"
        await say(
            f":white_check_mark: *Directory Changed*\n\n"
            f":file_folder: Current directory: `{relative_display}`"
            f"{resumed_session_info}"
        )

        # Log successful command
        if audit_logger:
            await audit_logger.log_command(user_id, "cd", [target_path], True)

    except Exception as e:
        error_msg = f":x: *Error changing directory*\n\n{str(e)}"
        await say(error_msg)

        # Log failed command
        if audit_logger:
            await audit_logger.log_command(user_id, "cd", [target_path], False)

        logger.error("Error in change_directory command", error=str(e), user_id=user_id)


async def print_working_directory(ack, say, command, client, context) -> None:
    """Handle /pwd command."""
    await ack()

    user_id = command["user_id"]
    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    user_state = _get_user_state(deps, user_id)

    current_dir = user_state.get("current_directory", settings.approved_directory)
    relative_path = current_dir.relative_to(settings.approved_directory)
    absolute_path = str(current_dir)

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":round_pushpin: *Current Directory*\n\n"
                    f"Relative: `{relative_path}/`\n"
                    f"Absolute: `{absolute_path}`"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":file_folder: List Files"},
                    "action_id": "action:ls",
                    "value": "ls",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":clipboard: Projects"},
                    "action_id": "action:show_projects",
                    "value": "show_projects",
                },
            ],
        },
    ]

    await say(
        text=f":round_pushpin: Current Directory: `{relative_path}/`",
        blocks=blocks,
    )


async def show_projects(ack, say, command, client, context) -> None:
    """Handle /projects command."""
    await ack()

    deps = context.get("deps", {})
    settings: Settings = deps["settings"]

    try:
        if settings.enable_project_threads:
            registry = deps.get("project_registry")
            manager = deps.get("project_threads_manager")
            if manager and getattr(manager, "registry", None):
                registry = manager.registry
            if not registry:
                await say(":x: *Project registry is not initialized.*")
                return

            projects = registry.list_enabled()
            if not projects:
                await say(
                    ":file_folder: *No Projects Found*\n\n"
                    "No enabled projects found in projects config."
                )
                return

            project_list = "\n".join(
                [
                    f"- *{escape_mrkdwn(p.name)}* "
                    f"(`{escape_mrkdwn(p.slug)}`) "
                    f"-> `{escape_mrkdwn(str(p.relative_path))}`"
                    for p in projects
                ]
            )

            await say(f":file_folder: *Configured Projects*\n\n{project_list}")
            return

        # Get directories in approved directory (these are "projects")
        projects = []
        for item in sorted(settings.approved_directory.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                projects.append(item.name)

        if not projects:
            await say(
                ":file_folder: *No Projects Found*\n\n"
                "No subdirectories found in your approved directory.\n"
                "Create some directories to organize your projects!"
            )
            return

        # Create Block Kit buttons with project names
        elements = []
        for project in projects:
            elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f":file_folder: {project}"},
                    "action_id": f"cd:{project}",
                    "value": project,
                }
            )

        # Add navigation buttons
        elements.extend([
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":house: Go to Root"},
                "action_id": "cd:/",
                "value": "/",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":arrows_counterclockwise: Refresh"},
                "action_id": "action:show_projects",
                "value": "show_projects",
            },
        ])

        project_list = "\n".join([f"- `{project}/`" for project in projects])

        # Slack limits actions block to 25 elements; chunk if needed
        action_blocks = []
        for i in range(0, len(elements), 25):
            action_blocks.append({"type": "actions", "elements": elements[i : i + 25]})

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":file_folder: *Available Projects*\n\n"
                        f"{project_list}\n\n"
                        f"Click a project below to navigate to it:"
                    ),
                },
            },
            *action_blocks,
        ]

        await say(
            text=f":file_folder: Available Projects\n\n{project_list}",
            blocks=blocks,
        )

    except Exception as e:
        await say(f":x: Error loading projects: {str(e)}")
        logger.error("Error in show_projects command", error=str(e))


async def session_status(ack, say, command, client, context) -> None:
    """Handle /status command."""
    await ack()

    user_id = command["user_id"]
    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    user_state = _get_user_state(deps, user_id)

    # Get session info
    claude_session_id = user_state.get("claude_session_id")
    current_dir = user_state.get("current_directory", settings.approved_directory)
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Get rate limiter info if available
    rate_limiter = deps.get("rate_limiter")
    usage_info = ""
    if rate_limiter:
        try:
            user_status = rate_limiter.get_user_status(user_id)
            cost_usage = user_status.get("cost_usage", {})
            current_cost = cost_usage.get("current", 0.0)
            cost_limit = cost_usage.get("limit", settings.claude_max_cost_per_user)
            cost_percentage = (current_cost / cost_limit) * 100 if cost_limit > 0 else 0

            usage_info = f":moneybag: Usage: ${current_cost:.2f} / ${cost_limit:.2f} ({cost_percentage:.0f}%)\n"
        except Exception:
            usage_info = ":moneybag: Usage: _Unable to retrieve_\n"

    # Check if there's a resumable session from the database
    resumable_info = ""
    if not claude_session_id:
        claude_integration: ClaudeIntegration = deps.get("claude_integration")
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                user_id, current_dir
            )
            if existing:
                resumable_info = (
                    f":arrows_counterclockwise: Resumable: `{existing.session_id[:8]}...` "
                    f"({existing.message_count} msgs)"
                )

    # Format status message
    status_lines = [
        ":bar_chart: *Session Status*",
        "",
        f":file_folder: Directory: `{relative_path}/`",
        f":robot_face: Claude Session: {'✅ Active' if claude_session_id else '❌ None'}",
        usage_info.rstrip(),
    ]

    if claude_session_id:
        status_lines.append(f":id: Session ID: `{claude_session_id[:8]}...`")
    elif resumable_info:
        status_lines.append(resumable_info)
        status_lines.append(":bulb: Session will auto-resume on your next message")

    status_text = "\n".join(status_lines)

    # Add action buttons
    elements = []
    if claude_session_id:
        elements.extend([
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":arrows_counterclockwise: Continue"},
                "action_id": "action:continue",
                "value": "continue",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":new: New Session"},
                "action_id": "action:new_session",
                "value": "new_session",
            },
        ])
    else:
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":new: Start Session"},
                "action_id": "action:new_session",
                "value": "new_session",
            }
        )

    elements.extend([
        {
            "type": "button",
            "text": {"type": "plain_text", "text": ":outbox_tray: Export"},
            "action_id": "action:export",
            "value": "export",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": ":arrows_counterclockwise: Refresh"},
            "action_id": "action:refresh_status",
            "value": "refresh_status",
        },
    ])

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": status_text}},
        {"type": "actions", "elements": elements},
    ]

    await say(text=status_text, blocks=blocks)


async def export_session(ack, say, command, client, context) -> None:
    """Handle /export command."""
    await ack()

    user_id = command["user_id"]
    deps = context.get("deps", {})
    features = deps.get("features")
    user_state = _get_user_state(deps, user_id)

    # Check if session export is available
    session_exporter = features.get_session_export() if features else None

    if not session_exporter:
        await say(
            ":outbox_tray: *Export Session*\n\n"
            "Session export functionality is not available.\n\n"
            "*Planned features:*\n"
            "- Export conversation history\n"
            "- Save session state\n"
            "- Share conversations\n"
            "- Create session backups"
        )
        return

    # Get current session
    claude_session_id = user_state.get("claude_session_id")

    if not claude_session_id:
        await say(
            ":x: *No Active Session*\n\n"
            "There's no active Claude session to export.\n\n"
            "*What you can do:*\n"
            "- Start a new session with `/new`\n"
            "- Continue an existing session with `/continue`\n"
            "- Check your status with `/status`"
        )
        return

    # Create export format selection buttons
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":outbox_tray: *Export Session*\n\n"
                    f"Ready to export session: `{claude_session_id[:8]}...`\n\n"
                    f"*Choose export format:*"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":memo: Markdown"},
                    "action_id": "export:markdown",
                    "value": "markdown",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":globe_with_meridians: HTML"},
                    "action_id": "export:html",
                    "value": "html",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":clipboard: JSON"},
                    "action_id": "export:json",
                    "value": "json",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":x: Cancel"},
                    "action_id": "export:cancel",
                    "value": "cancel",
                },
            ],
        },
    ]

    await say(
        text=f":outbox_tray: Export session `{claude_session_id[:8]}...`",
        blocks=blocks,
    )


async def end_session(ack, say, command, client, context) -> None:
    """Handle /end command to terminate the current session."""
    await ack()

    user_id = command["user_id"]
    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    user_state = _get_user_state(deps, user_id)

    # Check if there's an active session
    claude_session_id = user_state.get("claude_session_id")

    if not claude_session_id:
        await say(
            ":information_source: *No Active Session*\n\n"
            "There's no active Claude session to end.\n\n"
            "*What you can do:*\n"
            "- Use `/new` to start a new session\n"
            "- Use `/status` to check your session status\n"
            "- Send any message to start a conversation"
        )
        return

    # Get current directory for display
    current_dir = user_state.get("current_directory", settings.approved_directory)
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Clear session data
    user_state["claude_session_id"] = None
    user_state["session_started"] = False
    user_state["last_message"] = None

    # Create quick action buttons
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":white_check_mark: *Session Ended*\n\n"
                    f"Your Claude session has been terminated.\n\n"
                    f"*Current Status:*\n"
                    f"- Directory: `{relative_path}/`\n"
                    f"- Session: None\n"
                    f"- Ready for new commands\n\n"
                    f"*Next Steps:*\n"
                    f"- Start a new session with `/new`\n"
                    f"- Check status with `/status`\n"
                    f"- Send any message to begin a new conversation"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":new: New Session"},
                    "action_id": "action:new_session",
                    "value": "new_session",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":file_folder: Change Project"},
                    "action_id": "action:show_projects",
                    "value": "show_projects",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":bar_chart: Status"},
                    "action_id": "action:status",
                    "value": "status",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":question: Help"},
                    "action_id": "action:help",
                    "value": "help",
                },
            ],
        },
    ]

    await say(
        text=":white_check_mark: Session Ended",
        blocks=blocks,
    )

    logger.info("Session ended by user", user_id=user_id, session_id=claude_session_id)


async def quick_actions(ack, say, command, client, context) -> None:
    """Handle /actions command to show quick actions."""
    await ack()

    user_id = command["user_id"]
    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    features = deps.get("features")
    user_state = _get_user_state(deps, user_id)

    if not features or not features.is_enabled("quick_actions"):
        await say(
            ":x: *Quick Actions Disabled*\n\n"
            "Quick actions feature is not enabled.\n"
            "Contact your administrator to enable this feature."
        )
        return

    # Get current directory
    current_dir = user_state.get("current_directory", settings.approved_directory)

    try:
        quick_action_manager = features.get_quick_actions()
        if not quick_action_manager:
            await say(
                ":x: *Quick Actions Unavailable*\n\n"
                "Quick actions service is not available."
            )
            return

        # Get context-aware actions
        actions = await quick_action_manager.get_suggestions(
            session_data={"working_directory": str(current_dir), "user_id": user_id}
        )

        if not actions:
            await say(
                ":robot_face: *No Actions Available*\n\n"
                "No quick actions are available for the current context.\n\n"
                "*Try:*\n"
                "- Navigating to a project directory with `/cd`\n"
                "- Creating some code files\n"
                "- Starting a Claude session with `/new`"
            )
            return

        # Create Block Kit buttons from actions
        elements = []
        for action in actions:
            elements.append(
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"{action.icon} {action.name}",
                    },
                    "action_id": f"quick:{action.id}",
                    "value": action.id,
                }
            )

        relative_path = current_dir.relative_to(settings.approved_directory)
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":zap: *Quick Actions*\n\n"
                        f":file_folder: Context: `{relative_path}/`\n\n"
                        f"Select an action to execute:"
                    ),
                },
            },
            {"type": "actions", "elements": elements[:25]},
        ]

        await say(
            text=f":zap: Quick Actions for `{relative_path}/`",
            blocks=blocks,
        )

    except Exception as e:
        await say(f":x: *Error Loading Actions*\n\n{str(e)}")
        logger.error("Error in quick_actions command", error=str(e), user_id=user_id)


async def git_command(ack, say, command, client, context) -> None:
    """Handle /git command to show git repository information."""
    await ack()

    user_id = command["user_id"]
    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    features = deps.get("features")
    user_state = _get_user_state(deps, user_id)

    if not features or not features.is_enabled("git"):
        await say(
            ":x: *Git Integration Disabled*\n\n"
            "Git integration feature is not enabled.\n"
            "Contact your administrator to enable this feature."
        )
        return

    # Get current directory
    current_dir = user_state.get("current_directory", settings.approved_directory)

    try:
        git_integration = features.get_git_integration()
        if not git_integration:
            await say(
                ":x: *Git Integration Unavailable*\n\n"
                "Git integration service is not available."
            )
            return

        # Check if current directory is a git repository
        if not (current_dir / ".git").exists():
            await say(
                f":file_folder: *Not a Git Repository*\n\n"
                f"Current directory `{current_dir.relative_to(settings.approved_directory)}/` is not a git repository.\n\n"
                f"*Options:*\n"
                f"- Navigate to a git repository with `/cd`\n"
                f"- Initialize a new repository (ask Claude to help)\n"
                f"- Clone an existing repository (ask Claude to help)"
            )
            return

        # Get git status
        git_status = await git_integration.get_status(current_dir)

        # Format status message
        relative_path = current_dir.relative_to(settings.approved_directory)
        status_message = ":link: *Git Repository Status*\n\n"
        status_message += f":file_folder: Directory: `{relative_path}/`\n"
        status_message += f":herb: Branch: `{git_status.branch}`\n"

        if git_status.ahead > 0:
            status_message += f":arrow_up: Ahead: {git_status.ahead} commits\n"
        if git_status.behind > 0:
            status_message += f":arrow_down: Behind: {git_status.behind} commits\n"

        # Show file changes
        if not git_status.is_clean:
            status_message += "\n*Changes:*\n"
            if git_status.modified:
                status_message += f":pencil: Modified: {len(git_status.modified)} files\n"
            if git_status.added:
                status_message += f":heavy_plus_sign: Added: {len(git_status.added)} files\n"
            if git_status.deleted:
                status_message += f":heavy_minus_sign: Deleted: {len(git_status.deleted)} files\n"
            if git_status.untracked:
                status_message += f":grey_question: Untracked: {len(git_status.untracked)} files\n"
        else:
            status_message += "\n:white_check_mark: Working directory clean\n"

        # Create action buttons
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": status_message}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":bar_chart: Show Diff"},
                        "action_id": "git:diff",
                        "value": "diff",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":scroll: Show Log"},
                        "action_id": "git:log",
                        "value": "log",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":arrows_counterclockwise: Refresh"},
                        "action_id": "git:status",
                        "value": "status",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":file_folder: Files"},
                        "action_id": "action:ls",
                        "value": "ls",
                    },
                ],
            },
        ]

        await say(text=status_message, blocks=blocks)

    except Exception as e:
        await say(f":x: *Git Error*\n\n{str(e)}")
        logger.error("Error in git_command", error=str(e), user_id=user_id)


def _format_file_size(size: int) -> str:
    """Format file size in human-readable format."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}TB"

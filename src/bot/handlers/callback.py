"""Handle Slack Block Kit action callbacks."""

from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
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


def _get_deps(context: dict) -> Dict[str, Any]:
    """Get dependencies from context."""
    return context.get("deps", {})


def _get_user_state(deps: dict, user_id: str) -> dict:
    """Get per-user state dict."""
    user_states = deps.setdefault("_user_states", {})
    return user_states.setdefault(user_id, {})


async def handle_action(
    ack: Any, body: dict, say: Any, action: dict, client: Any, context: dict
) -> None:
    """Route Slack Block Kit action callbacks to appropriate handlers."""
    await ack()

    user_id = body["user"]["id"]
    action_id = action.get("action_id", "")
    value = action.get("value", "")

    logger.info("Processing action", user_id=user_id, action_id=action_id, value=value)

    deps = _get_deps(context)

    try:
        if action_id.startswith("cd_"):
            project_name = value or action_id.replace("cd_", "")
            await handle_cd_action(
                user_id, project_name, say, client, body, deps, context
            )
        elif action_id.startswith("action_"):
            action_type = action_id.replace("action_", "")
            await handle_general_action(
                user_id, action_type, say, client, body, deps, context
            )
        elif action_id.startswith("confirm_"):
            confirmation = action_id.replace("confirm_", "")
            await say(f"{'Confirmed' if confirmation == 'yes' else 'Cancelled'}.")
        elif action_id.startswith("quick_action_"):
            qa_id = value or action_id.replace("quick_action_", "")
            await handle_quick_action(user_id, qa_id, say, client, deps, context)
        elif action_id.startswith("followup_"):
            await say("Follow-up selected. Send your next message to continue.")
        elif action_id == "conversation_continue":
            await say("Ready to continue. Send your next message!")
        elif action_id == "conversation_end":
            user_state = _get_user_state(deps, user_id)
            user_state["claude_session_id"] = None
            await say("Session ended. Send a message to start a new one.")
        elif action_id.startswith("git_"):
            git_action = action_id.replace("git_", "")
            await handle_git_action(user_id, git_action, say, client, deps, context)
        elif action_id.startswith("export_"):
            export_format = value or action_id.replace("export_", "")
            await handle_export_action(
                user_id, export_format, say, client, deps, context
            )
        else:
            await say(f"Unknown action: `{escape_mrkdwn(action_id)}`")

    except Exception as e:
        logger.error(
            "Error handling action",
            error=str(e),
            user_id=user_id,
            action_id=action_id,
        )
        await say(f"Error processing action: {escape_mrkdwn(str(e))}")


async def handle_cd_action(
    user_id: str,
    project_name: str,
    say: Any,
    client: Any,
    body: dict,
    deps: dict,
    context: dict,
) -> None:
    """Handle directory change from Block Kit button."""
    settings: Settings = context.get("settings")
    security_validator = deps.get("security_validator")
    audit_logger = deps.get("audit_logger")
    claude_integration = deps.get("claude_integration")
    user_state = _get_user_state(deps, user_id)

    current_dir = user_state.get("current_directory", settings.approved_directory)
    directory_root = settings.approved_directory

    if project_name == "/":
        new_path = directory_root
    elif project_name == "..":
        new_path = current_dir.parent
        if not _is_within_root(new_path, directory_root):
            new_path = directory_root
    else:
        new_path = settings.approved_directory / project_name

    if security_validator:
        valid, resolved_path, error = security_validator.validate_path(
            str(new_path), settings.approved_directory
        )
        if not valid:
            await say(f"Access denied: {escape_mrkdwn(error)}")
            return
        new_path = resolved_path

    if not new_path.exists() or not new_path.is_dir():
        await say(f"Directory not found: `{escape_mrkdwn(project_name)}`")
        return

    user_state["current_directory"] = new_path

    session_info = ""
    if claude_integration:
        existing = await claude_integration._find_resumable_session(user_id, new_path)
        if existing:
            user_state["claude_session_id"] = existing.session_id
            session_info = f" · session resumed"
        else:
            user_state["claude_session_id"] = None

    is_git = (new_path / ".git").is_dir()
    git_badge = " (git)" if is_git else ""

    await say(f"Switched to `{escape_mrkdwn(project_name)}/`{git_badge}{session_info}")

    if audit_logger:
        await audit_logger.log_command(
            user_id=user_id, command="cd", args=[project_name], success=True
        )


async def handle_general_action(
    user_id: str,
    action_type: str,
    say: Any,
    client: Any,
    body: dict,
    deps: dict,
    context: dict,
) -> None:
    """Handle general action callbacks."""
    settings: Settings = context.get("settings")
    user_state = _get_user_state(deps, user_id)

    if action_type == "new_session":
        user_state["claude_session_id"] = None
        user_state["session_started"] = True
        await say("Session reset. What's next?")

    elif action_type == "status":
        current_dir = user_state.get("current_directory", settings.approved_directory)
        session_id = user_state.get("claude_session_id")
        session_status = "active" if session_id else "none"
        await say(f"Directory: `{current_dir}` · Session: {session_status}")

    elif action_type == "show_projects":
        entries = sorted(
            [
                d
                for d in settings.approved_directory.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            ],
            key=lambda d: d.name,
        )
        if not entries:
            await say("No projects found.")
            return

        lines = []
        elements = []
        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = ":package:" if is_git else ":file_folder:"
            lines.append(f"{icon} `{d.name}/`")
            elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": d.name},
                    "action_id": f"cd_{d.name}",
                    "value": d.name,
                }
            )

        blocks: List[dict] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Repos*\n\n" + "\n".join(lines)},
            },
        ]
        # Split buttons into rows of 5 (Slack max per actions block)
        for i in range(0, len(elements), 5):
            blocks.append({"type": "actions", "elements": elements[i : i + 5]})

        await say(text="Select a repo:", blocks=blocks)

    elif action_type == "ls":
        current_dir = user_state.get("current_directory", settings.approved_directory)
        items = []
        for item in sorted(current_dir.iterdir()):
            if item.name.startswith("."):
                continue
            if item.is_dir():
                items.append(f":file_folder: `{item.name}/`")
            else:
                items.append(f":page_facing_up: `{item.name}`")

        if not items:
            await say(f"`{current_dir}/` _(empty)_")
        else:
            await say(f"`{current_dir}/`\n\n" + "\n".join(items[:30]))

    else:
        await say(f"Action `{action_type}` is not implemented.")


async def handle_quick_action(
    user_id: str, action_id: str, say: Any, client: Any, deps: dict, context: dict
) -> None:
    """Handle quick action callbacks."""
    claude_integration = deps.get("claude_integration")
    settings: Settings = context.get("settings")
    user_state = _get_user_state(deps, user_id)

    if not claude_integration:
        await say("Claude integration not available.")
        return

    current_dir = user_state.get("current_directory", settings.approved_directory)

    action_prompts = {
        "test": "Run the project tests and report results.",
        "install": "Install project dependencies.",
        "format": "Format the code using the project's formatter.",
        "find_todos": "Find all TODO and FIXME comments in the codebase.",
        "build": "Build the project.",
        "git_status": "Show git status.",
        "lint": "Run the linter and report issues.",
    }

    prompt = action_prompts.get(action_id)
    if not prompt:
        await say(f"Unknown quick action: `{action_id}`")
        return

    await say(f"Running: {action_id}...")

    try:
        response = await claude_integration.run_command(
            prompt=prompt,
            working_directory=current_dir,
            user_id=user_id,
        )
        if response and response.content:
            content = response.content
            if len(content) > 3800:
                content = content[:3800] + "\n\n... _(truncated)_"
            await say(content)
    except Exception as e:
        await say(f"Error: {escape_mrkdwn(str(e))}")


async def handle_git_action(
    user_id: str, git_action: str, say: Any, client: Any, deps: dict, context: dict
) -> None:
    """Handle git-related action callbacks."""
    settings: Settings = context.get("settings")
    features = deps.get("features")
    user_state = _get_user_state(deps, user_id)

    if not features or not features.is_enabled("git"):
        await say("Git integration is not enabled.")
        return

    current_dir = user_state.get("current_directory", settings.approved_directory)
    git_integration = features.get_git_integration()

    if not git_integration:
        await say("Git integration unavailable.")
        return

    try:
        if git_action == "status":
            git_status = await git_integration.get_status(current_dir)
            await say(git_integration.format_status(git_status))
        elif git_action == "diff":
            diff = await git_integration.get_diff(current_dir)
            if not diff.strip():
                await say("No changes to show.")
            else:
                if len(diff) > 3500:
                    diff = diff[:3500] + "\n... (truncated)"
                await say(f"```\n{diff}\n```")
        elif git_action == "log":
            commits = await git_integration.get_file_history(current_dir, ".")
            if not commits:
                await say("No commits found.")
            else:
                lines = []
                for c in commits[:10]:
                    lines.append(f"• `{c.hash[:7]}` {c.message[:60]}")
                await say("*Git Log*\n\n" + "\n".join(lines))
        else:
            await say(f"Unknown git action: `{git_action}`")
    except Exception as e:
        await say(f"Git error: {escape_mrkdwn(str(e))}")


async def handle_export_action(
    user_id: str, export_format: str, say: Any, client: Any, deps: dict, context: dict
) -> None:
    """Handle export format selection callbacks."""
    if export_format == "cancel":
        await say("Export cancelled.")
        return

    features = deps.get("features")
    session_exporter = features.get_session_export() if features else None
    user_state = _get_user_state(deps, user_id)

    if not session_exporter:
        await say("Session export is not available.")
        return

    session_id = user_state.get("claude_session_id")
    if not session_id:
        await say("No active session to export.")
        return

    try:
        exported = await session_exporter.export_session(session_id, export_format)

        # Upload file to Slack
        await client.files_upload_v2(
            content=exported.content,
            filename=exported.filename,
            channel=context.get("channel_id", ""),
            initial_comment=f"Session export ({export_format.upper()})",
        )
    except Exception as e:
        await say(f"Export failed: {escape_mrkdwn(str(e))}")


def _format_file_size(size: int) -> str:
    """Format file size in human-readable format."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}TB"

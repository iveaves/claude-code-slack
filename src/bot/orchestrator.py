"""Message orchestrator -- single entry point for all Slack updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (slash commands, no complex Block Kit).
In classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from slack_bolt.app.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from ..claude.exceptions import ClaudeToolValidationError
from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from .utils.slack_format import escape_mrkdwn

logger = structlog.get_logger()

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "SlackFileUpload": "\U0001f4ce",
    "AskUserQuestion": "\u2753",
    "Skill": "\u26a1",
    "ScheduleJob": "\u23f0",
    "ListScheduledJobs": "\u23f0",
    "RemoveScheduledJob": "\u23f0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Slack updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps

    def _get_user_state(self, user_id: str) -> Dict[str, Any]:
        """Get or create per-user state dict (replaces context.user_data)."""
        user_states: Dict[str, Dict[str, Any]] = self.deps.setdefault(
            "_user_states", {}
        )
        return user_states.setdefault(user_id, {})

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into Bolt context.

        Bolt uses parameter name introspection to decide what to inject.
        We must declare all possible Bolt arg names so Bolt passes them.
        """

        async def wrapped(
            ack=None,
            say=None,
            event=None,
            command=None,
            body=None,
            action=None,
            client=None,
            context=None,
            respond=None,
            **kwargs: Any,
        ) -> None:
            if context is None:
                context = {}
            context["deps"] = self.deps
            context["settings"] = self.settings

            # Determine channel for routing
            source = event or command or {}
            channel_id = source.get("channel") or source.get("channel_id", "")

            # Extract user_id for user state
            user_id = (
                source.get("user")
                or source.get("user_id")
                or ((body or {}).get("user", {}).get("id", ""))
            )
            if user_id:
                context["user_state"] = self._get_user_state(user_id)
            else:
                context["user_state"] = {}

            is_start_bypass = handler.__name__ in {"agentic_start"}
            should_enforce = self.settings.enable_project_channels

            if should_enforce and not is_start_bypass:
                allowed = await self._apply_channel_routing_context(channel_id, context)
                if not allowed:
                    logger.warning(
                        "Channel routing rejected",
                        channel_id=channel_id,
                        user_id=user_id,
                        handler=handler.__name__,
                    )
                    if say:
                        await say("This channel is not configured for a project.")
                    return

            # Build kwargs dict with only the non-None Bolt args
            bolt_kwargs: Dict[str, Any] = {"context": context}
            if ack is not None:
                bolt_kwargs["ack"] = ack
            if say is not None:
                bolt_kwargs["say"] = say
            if event is not None:
                bolt_kwargs["event"] = event
            if command is not None:
                bolt_kwargs["command"] = command
            if body is not None:
                bolt_kwargs["body"] = body
            if action is not None:
                bolt_kwargs["action"] = action
            if client is not None:
                bolt_kwargs["client"] = client
            if respond is not None:
                bolt_kwargs["respond"] = respond
            bolt_kwargs.update(kwargs)

            try:
                await handler(**bolt_kwargs)
            finally:
                if should_enforce and not is_start_bypass:
                    self._persist_channel_state(channel_id, context)

        return wrapped

    async def _apply_channel_routing_context(
        self, channel_id: str, context: Dict[str, Any]
    ) -> bool:
        """Enforce strict project-channel routing and load channel-local state."""
        manager = self.deps.get("project_channels_manager")
        if manager is None:
            return False

        project = await manager.resolve_project(channel_id)
        if not project:
            return False

        user_state = context.get("user_state", {})
        channel_states = user_state.setdefault("channel_state", {})
        state = channel_states.get(channel_id, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        # Load last_response_ts from DB if not in memory (survives restarts)
        last_response_ts = state.get("last_response_ts")
        if not last_response_ts:
            repo = manager.repository
            last_response_ts = await repo.get_last_response_ts(channel_id)

        user_state["current_directory"] = current_dir
        user_state["claude_session_id"] = state.get("claude_session_id")
        user_state["last_response_ts"] = last_response_ts
        context["_channel_context"] = {
            "channel_id": channel_id,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
            "require_mention": project.require_mention,
        }
        return True

    def _persist_channel_state(self, channel_id: str, context: Dict[str, Any]) -> None:
        """Persist compatibility keys back into per-channel state."""
        channel_context = context.get("_channel_context")
        if not channel_context:
            return

        user_state = context.get("user_state", {})
        project_root = Path(channel_context["project_root"])
        current_dir = user_state.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        channel_states = user_state.setdefault("channel_state", {})
        prev = channel_states.get(channel_id, {})
        channel_states[channel_id] = {
            "current_directory": str(current_dir),
            "claude_session_id": user_state.get("claude_session_id"),
            "project_slug": channel_context["project_slug"],
            "last_response_ts": user_state.get(
                "last_response_ts", prev.get("last_response_ts")
            ),
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def register_handlers(self, app: AsyncApp) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: AsyncApp) -> None:
        """Register agentic handlers: slash commands + message/file events."""
        # Slash commands
        app.command("/start")(self._inject_deps(self.agentic_start))
        app.command("/new")(self._inject_deps(self.agentic_new))
        app.command("/stat")(self._inject_deps(self.agentic_status))
        app.command("/verbose")(self._inject_deps(self.agentic_verbose))
        app.command("/repo")(self._inject_deps(self.agentic_repo))

        if self.settings.enable_project_channels:
            app.command("/sync_channels")(self._inject_deps(self.agentic_sync_channels))

        # Message events (text)
        app.event("message")(self._inject_deps(self.agentic_text))

        # File shared events
        app.event("file_shared")(self._inject_deps(self.agentic_file))

        # Button actions (cd: prefix for project selection)
        app.action(re.compile(r"^cd_"))(self._inject_deps(self._agentic_callback))

        # AskUserQuestion response handler — wrap as plain function for Bolt introspection
        orchestrator = self

        async def _ask_user_action_handler(ack, body, action, client, say, **kwargs):
            await ack()
            action_id = action.get("action_id", "")
            value = action.get("value", "")

            if not action_id.startswith("ask_user_"):
                return

            parts = action_id[len("ask_user_") :].rsplit("_", 1)
            if len(parts) != 2:
                return

            interaction_key = parts[0]
            pending = orchestrator._pending_questions.get(interaction_key)
            if not pending:
                return

            # Update the original message: replace buttons with selection confirmation
            try:
                msg = body.get("message", {})
                msg_ts = msg.get("ts")
                channel_id = body.get("channel", {}).get("id") or body.get(
                    "container", {}
                ).get("channel_id", "")
                if msg_ts and channel_id:
                    await client.chat_update(
                        channel=channel_id,
                        ts=msg_ts,
                        text=f"Selected: *{value}*",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f":white_check_mark: Selected: *{value}*\n_Processing..._",
                                },
                            }
                        ],
                    )
            except Exception as e:
                logger.warning("Failed to update question message", error=str(e))

            questions = pending["questions"]
            if questions:
                question_text = questions[0].get("question", "")
                pending["answers"][question_text] = value

            pending["event"].set()

        app.action(re.compile(r"^ask_user_"))(_ask_user_action_handler)

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: AsyncApp) -> None:
        """Register full classic handler set."""
        from .handlers import callback, command, message

        # Slash commands
        classic_commands = [
            ("/start", command.start_command),
            ("/help", command.help_command),
            ("/new", command.new_session),
            ("/continue", command.continue_session),
            ("/end", command.end_session),
            ("/ls", command.list_files),
            ("/cd", command.change_directory),
            ("/pwd", command.print_working_directory),
            ("/projects", command.show_projects),
            ("/status", command.session_status),
            ("/export", command.export_session),
            ("/actions", command.quick_actions),
            ("/git", command.git_command),
        ]

        for cmd, handler in classic_commands:
            app.command(cmd)(self._inject_deps(handler))

        # Message events
        app.event("message")(self._inject_deps(message.handle_text_message))

        # File shared events
        app.event("file_shared")(self._inject_deps(message.handle_document))

        # All button actions
        app.action(re.compile(r".*"))(self._inject_deps(callback.handle_callback_query))

        logger.info("Classic handlers registered (13 commands + full handler set)")

    # --- Interactive AskUserQuestion support ---

    # Pending questions keyed by (channel, user_id) → asyncio.Event + answer dict
    _pending_questions: Dict[str, Dict[str, Any]] = {}

    def _make_scheduler_callback(
        self, channel: str, user_id: str, working_directory: Optional[Path] = None
    ) -> Optional[Callable]:
        """Create a callback that handles scheduler tool calls.

        Returns None if scheduler is not in deps (disables scheduler tools in prompt).
        """
        # Lazy lookup — scheduler is added to deps after bot.start()
        deps = self.deps

        if not deps.get("scheduler"):
            return None

        async def _handle_scheduler(tool_name: str, tool_input: Dict[str, Any]) -> str:
            scheduler = deps.get("scheduler")
            if not scheduler:
                return "Scheduler is not enabled."

            if tool_name == "ScheduleJob":
                job_name = tool_input.get("job_name", "Unnamed job")
                cron_expr = tool_input.get("cron_expression", "")
                prompt = tool_input.get("prompt", "")
                skill_name = tool_input.get("skill_name")

                if not cron_expr or not prompt:
                    return "Error: cron_expression and prompt are required."

                job_id = await scheduler.add_job(
                    job_name=job_name,
                    cron_expression=cron_expr,
                    prompt=prompt,
                    target_channel_ids=[channel],
                    working_directory=working_directory,
                    skill_name=skill_name,
                    created_by=user_id,
                )
                return (
                    f"Job scheduled successfully.\n"
                    f"Job ID: {job_id}\n"
                    f"Name: {job_name}\n"
                    f"Schedule: {cron_expr}\n"
                    f"Target channel: {channel}"
                )

            elif tool_name == "ListScheduledJobs":
                jobs = await scheduler.list_jobs()
                if not jobs:
                    return "No scheduled jobs."
                lines = []
                for j in jobs:
                    lines.append(
                        f"- {j.get('job_name', '?')} "
                        f"(ID: {j.get('job_id', '?')}, "
                        f"cron: {j.get('cron_expression', '?')})"
                    )
                return "Scheduled jobs:\n" + "\n".join(lines)

            elif tool_name == "RemoveScheduledJob":
                job_id = tool_input.get("job_id", "")
                if not job_id:
                    return "Error: job_id is required."
                await scheduler.remove_job(job_id)
                return f"Job {job_id} removed."

            return f"Unknown scheduler tool: {tool_name}"

        return _handle_scheduler

    def _make_reaction_callback(
        self, channel: str, message_ts: str, client: AsyncWebClient
    ) -> Callable:
        """Create a callback that adds/removes emoji reactions on the user's message."""

        async def _react(tool_input: Dict[str, Any]) -> str:
            emoji_name = tool_input.get("emoji_name", "")
            remove = tool_input.get("remove", False)

            if not emoji_name:
                return "Error: emoji_name is required."

            # Strip colons if provided (e.g. ":thumbsup:" → "thumbsup")
            emoji_name = emoji_name.strip(":")

            try:
                if remove:
                    await client.reactions_remove(
                        name=emoji_name, channel=channel, timestamp=message_ts
                    )
                    return f"Removed :{emoji_name}: reaction."
                else:
                    await client.reactions_add(
                        name=emoji_name, channel=channel, timestamp=message_ts
                    )
                    return f"Added :{emoji_name}: reaction."
            except Exception as e:
                error_str = str(e)
                if "already_reacted" in error_str:
                    return f"Already reacted with :{emoji_name}:."
                if "no_reaction" in error_str:
                    return f"No :{emoji_name}: reaction to remove."
                logger.warning(
                    "SlackReaction failed",
                    emoji=emoji_name,
                    error=error_str,
                )
                return f"Error: {error_str}"

        return _react

    def _make_file_upload_callback(
        self, channel: str, user_id: str, client: AsyncWebClient
    ) -> Callable:
        """Create a callback that uploads files to Slack on behalf of Claude.

        When Claude calls SlackFileUpload, this callback:
        1. Reads the file from disk (bypasses ToolMonitor path restrictions)
        2. Uploads it to the current Slack channel via files_upload_v2
        3. Returns a success/failure message to Claude
        """

        async def _upload_file(tool_input: Dict[str, Any]) -> str:
            file_path = tool_input.get("file_path", "")
            filename = tool_input.get("filename", "")
            comment = tool_input.get("comment", "")
            title = tool_input.get("title", "")

            if not file_path:
                return "Error: file_path is required."

            target = Path(file_path)

            # Resolve relative paths against approved directory
            if not target.is_absolute():
                target = self.settings.approved_directory / target

            target = target.resolve()

            if not target.is_file():
                return f"Error: File not found: {target}"

            # Safety: enforce max size (50 MB)
            file_size = target.stat().st_size
            max_upload = 50 * 1024 * 1024
            if file_size > max_upload:
                return (
                    f"Error: File too large ({file_size / 1024 / 1024:.1f} MB). "
                    f"Max upload size is {max_upload // 1024 // 1024} MB."
                )

            # Derive filename if not provided
            if not filename:
                filename = target.name

            try:
                result = await client.files_upload_v2(
                    file=str(target),
                    filename=filename,
                    channel=channel,
                    title=title or filename,
                    initial_comment=comment or None,
                )

                file_obj = result.get("file", {})
                permalink = file_obj.get("permalink", "uploaded")
                return (
                    f"File uploaded successfully to Slack.\n"
                    f"Filename: {filename}\n"
                    f"Size: {file_size / 1024:.1f} KB\n"
                    f"Link: {permalink}"
                )
            except Exception as e:
                logger.error(
                    "SlackFileUpload failed",
                    error=str(e),
                    file_path=str(target),
                    channel=channel,
                    user_id=user_id,
                )
                return f"Error uploading file to Slack: {e}"

        return _upload_file

    def _make_ask_user_callback(
        self, channel: str, user_id: str, client: AsyncWebClient
    ) -> Callable:
        """Create a callback that posts AskUserQuestion to Slack and waits for response.

        When Claude calls AskUserQuestion, this callback:
        1. Posts the questions as Block Kit buttons in Slack
        2. Waits for the user to click a button (via asyncio.Event)
        3. Returns the answers dict to the SDK
        """

        async def _ask_user(tool_input: Dict[str, Any]) -> Dict[str, str]:
            questions = tool_input.get("questions", [])
            if not questions:
                return {}

            # Create a unique key for this interaction
            interaction_key = f"{channel}:{user_id}"
            wait_event = asyncio.Event()
            self._pending_questions[interaction_key] = {
                "event": wait_event,
                "answers": {},
                "questions": questions,
            }

            # Build Block Kit for each question with rich context
            blocks: List[Dict[str, Any]] = []
            for q in questions:
                question_text = q.get("question", "")
                header = q.get("header", "")
                options = q.get("options", [])

                # Header block
                if header:
                    blocks.append(
                        {
                            "type": "header",
                            "text": {"type": "plain_text", "text": header},
                        }
                    )

                # Question text
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": question_text},
                    }
                )

                blocks.append({"type": "divider"})

                # Render each option with description and optional preview
                if options:
                    elements = []
                    for i, opt in enumerate(options):
                        label = opt.get("label", f"Option {i+1}")
                        desc = opt.get("description", "")
                        markdown_preview = opt.get("markdown", "")

                        # Show option details as a section with description
                        if desc or markdown_preview:
                            option_text = f"*{escape_mrkdwn(label)}*"
                            if desc:
                                option_text += f"\n{escape_mrkdwn(desc)}"

                            # Add markdown preview as a code block if present
                            if markdown_preview:
                                # Truncate long previews for Slack's 3000 char block limit
                                preview = markdown_preview[:2000]
                                option_text += f"\n```\n{preview}\n```"

                            button: Dict[str, Any] = {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": f"Select: {label}"[:75],
                                },
                                "action_id": f"ask_user_{interaction_key}_{i}",
                                "value": label,
                            }
                            if i == 0:
                                button["style"] = "primary"

                            blocks.append(
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": option_text[:3000],
                                    },
                                    "accessory": button,
                                }
                            )
                        else:
                            # No description — just collect as a plain button
                            elements.append(
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": label[:75]},
                                    "action_id": f"ask_user_{interaction_key}_{i}",
                                    "value": label,
                                }
                            )

                    # If we have leftover plain buttons (no descriptions), render as actions block
                    if elements:
                        for j in range(0, len(elements), 5):
                            blocks.append(
                                {
                                    "type": "actions",
                                    "elements": elements[j : j + 5],
                                }
                            )

                blocks.append({"type": "divider"})

            # Post the question to Slack
            try:
                await client.chat_postMessage(
                    channel=channel,
                    text="Claude needs your input:",
                    blocks=blocks,
                )
            except Exception as e:
                logger.error("Failed to post AskUserQuestion to Slack", error=str(e))
                del self._pending_questions[interaction_key]
                return {}

            # Wait for user response (timeout after 5 minutes)
            try:
                await asyncio.wait_for(wait_event.wait(), timeout=300)
            except asyncio.TimeoutError:
                logger.warning(
                    "AskUserQuestion timed out", channel=channel, user_id=user_id
                )
                del self._pending_questions[interaction_key]
                return {}

            answers = self._pending_questions[interaction_key]["answers"]
            del self._pending_questions[interaction_key]
            return answers

        return _ask_user

    async def _handle_ask_user_response(
        self, ack: Callable, body: Dict[str, Any], action: Dict[str, Any], **kwargs: Any
    ) -> None:
        """Handle user's button click response to AskUserQuestion."""
        await ack()

        action_id = action.get("action_id", "")
        value = action.get("value", "")

        # Parse the interaction key from action_id: ask_user_{channel}:{user_id}_{option_index}
        if not action_id.startswith("ask_user_"):
            return

        # Extract interaction key (everything between "ask_user_" and the last "_N")
        parts = action_id[len("ask_user_") :].rsplit("_", 1)
        if len(parts) != 2:
            return

        interaction_key = parts[0]
        pending = self._pending_questions.get(interaction_key)
        if not pending:
            return

        # Find which question this answer belongs to
        questions = pending["questions"]
        if questions:
            # Map the answer: question_text → selected label
            question_text = questions[0].get("question", "")
            pending["answers"][question_text] = value

        # Signal that we got an answer
        pending["event"].set()

    # --- Agentic handlers ---

    async def agentic_start(
        self,
        ack: Callable,
        say: Callable,
        command: Dict[str, Any],
        client: AsyncWebClient,
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Brief welcome, no buttons."""
        await ack()
        user_id = command["user_id"]
        user_state = context.get("user_state", {})

        current_dir = user_state.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"`{current_dir}/`"

        # Fetch user info for display name
        try:
            user_info = await client.users_info(user=user_id)
            user_name = escape_mrkdwn(
                user_info["user"]["profile"].get("first_name")
                or user_info["user"].get("real_name", "there")
            )
        except Exception:
            user_name = "there"

        await say(
            f"Hi {user_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need -- I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) - /status"
        )

    async def agentic_new(
        self,
        ack: Callable,
        say: Callable,
        command: Dict[str, Any],
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Reset session, one-line confirmation."""
        await ack()
        user_state = context.get("user_state", {})
        user_state["claude_session_id"] = None
        user_state["session_started"] = True
        user_state["force_new_session"] = True

        await say("Session reset. What's next?")

    async def agentic_status(
        self,
        ack: Callable,
        say: Callable,
        command: Dict[str, Any],
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Compact one-line status, no buttons."""
        await ack()
        user_id = command["user_id"]
        user_state = context.get("user_state", {})

        current_dir = user_state.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = str(current_dir)

        session_id = user_state.get("claude_session_id")
        session_status = "active" if session_id else "none"

        # Cost info
        cost_str = ""
        rate_limiter = self.deps.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(user_id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" - Cost: ${current_cost:.2f}"
            except Exception:
                pass

        await say(f":file_folder: {dir_display} - Session: {session_status}{cost_str}")

    def _get_verbose_level(self, user_state: Dict[str, Any]) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = user_state.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self,
        ack: Callable,
        say: Callable,
        command: Dict[str, Any],
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        await ack()
        user_state = context.get("user_state", {})
        args_text = command.get("text", "").strip()
        args = args_text.split() if args_text else []

        if not args:
            current = self._get_verbose_level(user_state)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await say(
                f"Verbosity: *{current}* ({labels.get(current, '?')})\n\n"
                "Usage: `/verbose 0|1|2`\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)"
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await say("Please use: /verbose 0, /verbose 1, or /verbose 2")
            return

        user_state["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await say(f"Verbosity set to *{level}* ({labels[level]})")

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "thinking":
                # Claude's internal thinking — show with thought bubble
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ad {snippet}")
                else:
                    lines.append(f"\U0001f4ad {snippet[:80]}")
            elif kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry.get("name", "unknown"))
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry.get('name', 'unknown')}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    def _make_stream_callback(
        self,
        verbose_level: int,
        client: AsyncWebClient,
        channel: str,
        progress_ts: str,
        tool_log: List[Dict[str, Any]],
        start_time: float,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        Returns None when verbose_level is 0 (nothing to display).
        Updates the Slack progress message via chat_update.
        """
        if verbose_level == 0:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    tool_log.append({"kind": "tool", "name": name, "detail": detail})

            # Capture thinking (shown with thought bubble emoji)
            if update_obj.type == "thinking" and update_obj.content:
                text = update_obj.content.strip()
                if text and verbose_level >= 1:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        tool_log.append(
                            {"kind": "thinking", "detail": first_line[:120]}
                        )

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text and verbose_level >= 1:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        tool_log.append({"kind": "text", "detail": first_line[:120]})

            # Throttle progress message edits to avoid Slack rate limits
            now = time.time()
            if (now - last_edit_time[0]) >= 2.0 and tool_log:
                last_edit_time[0] = now
                new_text = self._format_verbose_progress(
                    tool_log, verbose_level, start_time
                )
                try:
                    await client.chat_update(
                        channel=channel, ts=progress_ts, text=new_text
                    )
                except Exception:
                    pass

        return _on_stream

    async def run_scheduled_prompt(
        self,
        prompt: str,
        channel_id: str,
        user_id: str,
        client: AsyncWebClient,
    ) -> None:
        """Execute a scheduled job's prompt through the same flow as agentic_text.

        This shares the channel's Claude session so the job and user
        conversation have full mutual context.
        """
        # Resolve channel to project/working directory
        user_state = self._get_user_state(user_id)
        context: Dict[str, Any] = {
            "user_state": user_state,
            "deps": self.deps,
            "settings": self.settings,
        }

        manager = self.deps.get("project_channels_manager")
        if manager:
            project = await manager.resolve_project(channel_id)
            if project:
                channel_states = user_state.setdefault("channel_state", {})
                state = channel_states.get(channel_id, {})
                current_dir = state.get("current_directory")
                if current_dir:
                    current_dir = Path(current_dir).resolve()
                    if not current_dir.is_dir():
                        current_dir = project.absolute_path
                else:
                    current_dir = project.absolute_path
                user_state["current_directory"] = current_dir
                user_state["claude_session_id"] = state.get("claude_session_id")

        current_dir = user_state.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = user_state.get("claude_session_id")

        claude_integration = self.deps.get("claude_integration")
        if not claude_integration:
            logger.error("Claude integration not available for scheduled prompt")
            return

        verbose_level = self.settings.verbose_level
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()

        # Post progress message
        result = await client.chat_postMessage(channel=channel_id, text="Working...")
        progress_ts = result["ts"]

        on_stream = self._make_stream_callback(
            verbose_level, client, channel_id, progress_ts, tool_log, start_time
        )

        try:
            ask_user_cb = self._make_ask_user_callback(channel_id, user_id, client)
            scheduler_cb = self._make_scheduler_callback(
                channel_id, user_id, working_directory=current_dir
            )
            file_upload_cb = self._make_file_upload_callback(
                channel_id, user_id, client
            )

            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                ask_user_callback=ask_user_cb,
                scheduler_callback=scheduler_cb,
                file_upload_callback=file_upload_cb,
            )

            user_state["claude_session_id"] = claude_response.session_id

            # Persist channel state
            if manager:
                channel_states = user_state.setdefault("channel_state", {})
                channel_states[channel_id] = {
                    "current_directory": str(current_dir),
                    "claude_session_id": claude_response.session_id,
                }

            from .utils.formatting import ResponseFormatter

            response_text = claude_response.content or ""
            if not response_text.strip():
                logger.warning(
                    "Scheduled prompt returned empty content",
                    session_id=claude_response.session_id,
                    num_turns=claude_response.num_turns,
                )
                response_text = (
                    "_Scheduled job completed but returned no text output._"
                )

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(response_text)

            # Delete progress message
            try:
                await client.chat_delete(channel=channel_id, ts=progress_ts)
            except Exception:
                pass

            for i, message in enumerate(formatted_messages):
                await client.chat_postMessage(channel=channel_id, text=message.text)
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

        except Exception as e:
            logger.exception(
                "Scheduled prompt execution failed",
                channel_id=channel_id,
                error=str(e),
            )
            try:
                await client.chat_update(
                    channel=channel_id,
                    ts=progress_ts,
                    text=f"Scheduled job failed: {e}",
                )
            except Exception:
                pass

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
    _ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".bz2", ".xz", ".tgz"}

    async def _download_slack_files(
        self, files: List[Dict[str, Any]], client: AsyncWebClient
    ) -> List[Dict[str, Any]]:
        """Download files from Slack and save to a temp directory.

        Returns list of dicts: {"path": str, "name": str, "category": str}
        where category is "image", "text", "archive", or "binary".
        """
        import tempfile

        import aiohttp

        saved: List[Dict[str, Any]] = []
        tmp_dir = Path(tempfile.gettempdir()) / "claude-slack-files"
        tmp_dir.mkdir(exist_ok=True)

        for f in files:
            name = f.get("name", "file")
            ext = Path(name).suffix.lower()

            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue

            try:
                headers = {"Authorization": f"Bearer {client.token}"}
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.read()

                dest = tmp_dir / f"{f.get('id', 'file')}_{name}"
                dest.write_bytes(data)

                # Categorize the file
                if ext in self._IMAGE_EXTS:
                    category = "image"
                elif ext in self._ARCHIVE_EXTS:
                    category = "archive"
                else:
                    # Probe for text vs binary
                    try:
                        data.decode("utf-8")
                        category = "text"
                    except UnicodeDecodeError:
                        category = "binary"

                saved.append({"path": str(dest), "name": name, "category": category})
                logger.info(
                    "Downloaded Slack file",
                    filename=name,
                    category=category,
                    path=str(dest),
                )
            except Exception as e:
                logger.warning(
                    "Failed to download Slack file", filename=name, error=str(e)
                )

        return saved

    def _build_file_prompt(self, downloaded: List[Dict[str, Any]]) -> str:
        """Build a Claude prompt section for downloaded files by category."""
        parts: List[str] = []
        for f in downloaded:
            path, name, cat = f["path"], f["name"], f["category"]
            if cat == "image":
                parts.append(f"- `{path}` (image — use Read tool to view)")
            elif cat == "text":
                # Inline small text files, reference large ones
                try:
                    content = Path(path).read_text(encoding="utf-8", errors="ignore")
                    if len(content) > 50000:
                        content = content[:50000] + "\n... (truncated)"
                    parts.append(f"File `{name}`:\n```\n{content}\n```")
                except Exception:
                    parts.append(f"- `{path}` (text file — use Read tool)")
            elif cat == "archive":
                summary = self._extract_archive_summary(path)
                parts.append(f"Archive `{name}`:\n{summary}")
            else:
                # binary (PDF, docx, etc.) — Claude can Read PDFs natively
                parts.append(f"- `{path}` (binary file — use Read tool to inspect)")

        return "\n\nThe user attached these files:\n" + "\n".join(parts)

    def _extract_archive_summary(self, archive_path: str) -> str:
        """Extract a zip/tar archive to temp dir and return a file tree summary."""
        import shutil
        import tarfile
        import uuid
        import zipfile

        arc = Path(archive_path)
        extract_dir = arc.parent / f"extract_{uuid.uuid4().hex[:8]}"
        extract_dir.mkdir(exist_ok=True)

        try:
            if arc.suffix == ".zip":
                with zipfile.ZipFile(arc) as zf:
                    total = sum(i.file_size for i in zf.filelist)
                    if total > 100 * 1024 * 1024:
                        return "(archive too large to extract — >100MB)"
                    for info in zf.filelist:
                        fp = Path(info.filename)
                        if fp.is_absolute() or ".." in fp.parts:
                            continue
                        target = extract_dir / fp
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if not info.is_dir():
                            with zf.open(info) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
            elif arc.suffix in {".tar", ".gz", ".bz2", ".xz", ".tgz"}:
                with tarfile.open(arc) as tf:
                    total = sum(m.size for m in tf.getmembers())
                    if total > 100 * 1024 * 1024:
                        return "(archive too large to extract — >100MB)"
                    for member in tf.getmembers():
                        if member.name.startswith("/") or ".." in member.name:
                            continue
                        tf.extract(member, extract_dir)
            else:
                return f"(unsupported archive format: {arc.suffix})"

            # Build file tree
            lines: List[str] = []
            for item in sorted(extract_dir.rglob("*")):
                if item.is_file():
                    rel = item.relative_to(extract_dir)
                    size = item.stat().st_size
                    lines.append(f"  {rel} ({size} bytes)")
            tree = "\n".join(lines[:100])  # cap at 100 entries
            if len(lines) > 100:
                tree += f"\n  ... and {len(lines) - 100} more files"

            # Provide the extract path so Claude can Read individual files
            return (
                f"Extracted to `{extract_dir}` ({len(lines)} files):\n"
                f"```\n{tree}\n```\n"
                f"Use Read tool on files inside `{extract_dir}` to inspect contents."
            )
        except Exception as e:
            return f"(failed to extract archive: {e})"

    async def _fetch_gap_context(
        self,
        channel: str,
        last_response_ts: Optional[str],
        client: AsyncWebClient,
    ) -> Optional[str]:
        """Fetch channel messages since the bot's last response.

        Returns a formatted summary of the gap conversation, or None if
        there's nothing to catch up on. When last_response_ts is None
        (e.g. after a restart), fetches the most recent messages for
        initial context.
        """
        if not last_response_ts:
            return None

        try:
            result = await client.conversations_history(
                channel=channel,
                oldest=last_response_ts,
                limit=50,
                inclusive=False,
            )
            messages = result.get("messages", [])
            if not messages:
                return None

            # Filter out bot messages — only include human conversation
            human_msgs = [
                m for m in messages if not m.get("bot_id") and m.get("text", "").strip()
            ]
            if not human_msgs:
                return None

            # Oldest first (Slack returns newest first)
            human_msgs.reverse()

            lines: List[str] = []
            for m in human_msgs:
                user = m.get("user", "someone")
                text = m.get("text", "")
                lines.append(f"<@{user}>: {text}")

            context_block = "\n".join(lines)
            return (
                "Here is the recent conversation in this channel "
                "since your last response (for context):\n\n"
                f"{context_block}\n\n"
                "Now the user is addressing you directly:\n"
            )
        except Exception as e:
            logger.warning("Failed to fetch gap context", error=str(e))
            return None

    async def _fetch_thread_parent(
        self,
        channel: str,
        thread_ts: str,
        client: AsyncWebClient,
    ) -> Optional[str]:
        """Fetch the parent message of a thread for context.

        Returns a short context preamble, or None if unavailable.
        """
        try:
            result = await client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=1,
                inclusive=True,
            )
            messages = result.get("messages", [])
            if not messages:
                return None

            parent = messages[0]
            user = parent.get("user", "someone")
            text = parent.get("text", "")
            if not text:
                return None

            return (
                f"(This is a reply in a thread. The parent message "
                f'from <@{user}> was: "{text}")\n\n'
            )
        except Exception as e:
            logger.warning("Failed to fetch thread parent", error=str(e))
            return None

    def _check_mention_required(
        self, message_text: str, channel: str, context: Dict[str, Any]
    ) -> Optional[str]:
        """Check if this channel requires a mention to respond.

        Returns the cleaned message (trigger stripped) if bot should respond,
        or None if the message should be ignored. "Pan" can appear anywhere
        in the message, not just at the start.
        """
        chan_ctx = context.get("_channel_context", {})
        if not chan_ctx.get("require_mention"):
            return message_text  # no filtering needed

        # DMs always respond
        if channel.startswith("D"):
            return message_text

        stripped = message_text.lstrip()
        lower = stripped.lower()

        # Check for bot name anywhere in message (case-insensitive, word boundary)
        bot_name = self.settings.bot_name.lower()
        name_pattern = r"\b" + re.escape(bot_name) + r"\b"
        name_match = re.search(name_pattern, lower)
        if name_match:
            # Remove the trigger word from the message
            start, end = name_match.start(), name_match.end()
            cleaned = (stripped[:start] + stripped[end:]).strip(" ,:;-")
            return cleaned or message_text

        # Check for Slack @mention (<@U...>) anywhere
        if re.search(r"<@U[A-Z0-9]+>", stripped):
            remainder = re.sub(r"<@U[A-Z0-9]+>\s*", "", stripped).strip()
            return remainder or message_text

        return None  # ignore this message

    async def agentic_text(
        self,
        event: Dict[str, Any],
        say: Callable,
        client: AsyncWebClient,
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        # Ignore bot messages, message_changed events, etc.
        # Allow file_share subtype through so images/files attached to messages are processed.
        subtype = event.get("subtype")
        if subtype is not None and subtype != "file_share":
            return

        user_id = event.get("user", "")
        message_text = event.get("text", "")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts")

        # For file_share events, text may be empty — that's OK if there are files
        files = event.get("files", [])
        if not user_id or (not message_text and not files):
            return

        # If channel requires mention, check and strip trigger prefix
        # This applies to both top-level messages and thread replies
        checked = self._check_mention_required(message_text or "", channel, context)
        if checked is None:
            return  # not addressed to us
        message_text = checked

        # For require_mention channels, fetch gap conversation for context
        chan_ctx = context.get("_channel_context", {})
        if chan_ctx.get("require_mention") and not channel.startswith("D"):
            user_state_pre = context.get("user_state", {})
            # Check flat key first (loaded from DB on restart), then nested channel state
            last_ts = user_state_pre.get("last_response_ts")
            if not last_ts:
                ch_state = user_state_pre.get("channel_state", {}).get(channel, {})
                last_ts = ch_state.get("last_response_ts")
            gap = await self._fetch_gap_context(channel, last_ts, client)
            if gap:
                message_text = gap + message_text

        # If message has files, download and build appropriate prompts
        if files:
            downloaded = await self._download_slack_files(files, client)
            if downloaded:
                file_prompt = self._build_file_prompt(downloaded)
                message_text = (
                    message_text or "Please analyze these files."
                ) + file_prompt

        user_state = context.get("user_state", {})

        # If replying in a thread, fetch the parent message for context
        if thread_ts:
            parent_context = await self._fetch_thread_parent(channel, thread_ts, client)
            if parent_context:
                message_text = parent_context + message_text

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
            thread_ts=thread_ts,
        )

        # Wrap say() to reply in the same thread when applicable
        _say = say
        if thread_ts:

            async def _say(text="", **kw):
                return await say(text=text, thread_ts=thread_ts, **kw)

        # Rate limit check
        rate_limiter = self.deps.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await _say(f":hourglass: {limit_message}")
                return

        verbose_level = self._get_verbose_level(user_state)

        # Post initial progress message
        result = await _say("Working...")
        progress_ts = result["ts"]
        progress_channel = result["channel"]

        claude_integration = self.deps.get("claude_integration")
        if not claude_integration:
            await client.chat_update(
                channel=progress_channel,
                ts=progress_ts,
                text="Claude integration not available. Check configuration.",
            )
            return

        current_dir = user_state.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = user_state.get("claude_session_id")

        # Check if /new was used -- skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(user_state.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        on_stream = self._make_stream_callback(
            verbose_level, client, progress_channel, progress_ts, tool_log, start_time
        )

        success = True
        try:
            # Create interactive callbacks for Slack
            ask_user_cb = self._make_ask_user_callback(channel, user_id, client)
            scheduler_cb = self._make_scheduler_callback(
                channel, user_id, working_directory=current_dir
            )
            file_upload_cb = self._make_file_upload_callback(channel, user_id, client)
            # React to the user's original message (not the progress msg)
            user_message_ts = event.get("ts", "")
            reaction_cb = self._make_reaction_callback(
                channel, user_message_ts, client
            )

            claude_response = await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                ask_user_callback=ask_user_cb,
                scheduler_callback=scheduler_cb,
                file_upload_callback=file_upload_cb,
                reaction_callback=reaction_cb,
            )

            # New session created successfully -- clear the one-shot flag
            if force_new:
                user_state["force_new_session"] = False

            user_state["claude_session_id"] = claude_response.session_id

            # Track directory changes
            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            # Store interaction
            storage = self.deps.get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Format response — guard against empty content so Slack
            # doesn't reject the message with 'no_text'.
            from .utils.formatting import FormattedMessage, ResponseFormatter

            response_text = claude_response.content or ""
            if not response_text.strip():
                logger.warning(
                    "Claude returned empty content",
                    session_id=claude_response.session_id,
                    num_turns=claude_response.num_turns,
                    cost=claude_response.cost,
                )
                response_text = "_Claude completed the request but returned no text output._"

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(response_text)

        except ClaudeToolValidationError as e:
            success = False
            logger.error("Tool validation error", error=str(e), user_id=user_id)
            from .utils.formatting import FormattedMessage

            formatted_messages = [FormattedMessage(str(e))]

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [FormattedMessage(_format_error_message(str(e)))]

        # Delete the progress message
        try:
            await client.chat_delete(channel=progress_channel, ts=progress_ts)
        except Exception:
            pass

        last_say_ts: Optional[str] = None
        for i, message in enumerate(formatted_messages):
            try:
                say_result = await _say(text=message.text)
                if say_result and hasattr(say_result, "get"):
                    last_say_ts = say_result.get("ts")
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send response",
                    error=str(e),
                    message_index=i,
                )
                try:
                    await _say(text="Failed to send response. Please try again.")
                except Exception:
                    pass

        # Store timestamp of bot's last response for gap context (memory + DB)
        if last_say_ts:
            user_state["last_response_ts"] = last_say_ts
            # Persist to DB so it survives restarts
            manager = self.deps.get("project_channels_manager")
            if manager and channel:
                try:
                    await manager.repository.set_last_response_ts(channel, last_say_ts)
                except Exception:
                    pass  # non-critical

        # Audit log
        audit_logger = self.deps.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_file(
        self,
        event: Dict[str, Any],
        say: Callable,
        client: AsyncWebClient,
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = event.get("user_id") or event.get("user", "")
        file_id = event.get("file_id", "")
        channel = event.get("channel_id") or event.get("channel", "")

        if not user_id or not file_id:
            return

        # In require_mention channels, ignore standalone file uploads
        chan_ctx = context.get("_channel_context", {})
        if chan_ctx.get("require_mention") and not channel.startswith("D"):
            return

        user_state = context.get("user_state", {})

        logger.info(
            "Agentic file upload",
            user_id=user_id,
            file_id=file_id,
        )

        # Fetch file info from Slack
        try:
            file_info_resp = await client.files_info(file=file_id)
            file_info = file_info_resp["file"]
        except Exception as e:
            await say(f"Could not retrieve file info: {e}")
            return

        filename = file_info.get("name", "unknown")
        file_size = file_info.get("size", 0)

        # Security validation
        security_validator = self.deps.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(filename)
            if not valid:
                await say(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if file_size > max_size:
            await say(f"File too large ({file_size / 1024 / 1024:.1f}MB). Max: 10MB.")
            return

        # Post progress message
        result = await say("Working...")
        progress_ts = result["ts"]
        progress_channel = result["channel"]

        # Download file content
        prompt: Optional[str] = None
        try:
            url_private = file_info.get("url_private", "")
            if url_private:
                import tempfile

                import aiohttp

                headers = {"Authorization": f"Bearer {client.token}"}
                async with aiohttp.ClientSession() as session:
                    async with session.get(url_private, headers=headers) as resp:
                        file_bytes = await resp.read()

                ext = Path(filename).suffix.lower()

                if ext in self._ARCHIVE_EXTS:
                    # Save archive and extract summary
                    tmp_dir = Path(tempfile.gettempdir()) / "claude-slack-files"
                    tmp_dir.mkdir(exist_ok=True)
                    dest = tmp_dir / f"{file_id}_{filename}"
                    dest.write_bytes(file_bytes)
                    summary = self._extract_archive_summary(str(dest))
                    prompt = f"The user uploaded an archive: `{filename}`\n\n{summary}"
                else:
                    # Try text first, fall back to saving binary to disk
                    try:
                        content = file_bytes.decode("utf-8")
                        if len(content) > 50000:
                            content = content[:50000] + "\n... (truncated)"
                        prompt = (
                            f"Please review this file:\n\n*File:* `{filename}`\n\n"
                            f"```\n{content}\n```"
                        )
                    except UnicodeDecodeError:
                        # Binary file — save to temp and let Claude Read it
                        tmp_dir = Path(tempfile.gettempdir()) / "claude-slack-files"
                        tmp_dir.mkdir(exist_ok=True)
                        dest = tmp_dir / f"{file_id}_{filename}"
                        dest.write_bytes(file_bytes)
                        prompt = (
                            f"The user uploaded a file: `{filename}`\n"
                            f"Saved to: `{dest}`\n"
                            f"Use the Read tool to inspect this file."
                        )
            else:
                await client.chat_update(
                    channel=progress_channel,
                    ts=progress_ts,
                    text="Could not download file. Missing URL.",
                )
                return
        except Exception as e:
            await client.chat_update(
                channel=progress_channel,
                ts=progress_ts,
                text=f"Failed to download file: {e}",
            )
            return

        # Process with Claude
        claude_integration = self.deps.get("claude_integration")
        if not claude_integration:
            await client.chat_update(
                channel=progress_channel,
                ts=progress_ts,
                text="Claude integration not available. Check configuration.",
            )
            return

        current_dir = user_state.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = user_state.get("claude_session_id")

        # Check if /new was used
        force_new = bool(user_state.get("force_new_session"))

        verbose_level = self._get_verbose_level(user_state)
        tool_log: List[Dict[str, Any]] = []
        on_stream = self._make_stream_callback(
            verbose_level, client, progress_channel, progress_ts, tool_log, time.time()
        )

        try:
            # Create interactive callbacks (same as agentic_text)
            ask_user_cb = self._make_ask_user_callback(channel, user_id, client)
            scheduler_cb = self._make_scheduler_callback(
                channel, user_id, working_directory=current_dir
            )
            file_upload_cb = self._make_file_upload_callback(channel, user_id, client)

            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                ask_user_callback=ask_user_cb,
                scheduler_callback=scheduler_cb,
                file_upload_callback=file_upload_cb,
            )

            if force_new:
                user_state["force_new_session"] = False

            user_state["claude_session_id"] = claude_response.session_id

            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            # Delete progress message
            try:
                await client.chat_delete(channel=progress_channel, ts=progress_ts)
            except Exception:
                pass

            for i, message in enumerate(formatted_messages):
                await say(text=message.text)
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

        except Exception as e:
            from .handlers.message import _format_error_message

            await client.chat_update(
                channel=progress_channel,
                ts=progress_ts,
                text=_format_error_message(str(e)),
            )
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)

    async def agentic_repo(
        self,
        ack: Callable,
        say: Callable,
        command: Dict[str, Any],
        client: AsyncWebClient,
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          -- list subdirectories with git indicators
        /repo <name>   -- switch to that directory, resume session if available
        """
        await ack()
        user_id = command["user_id"]
        user_state = context.get("user_state", {})
        args_text = command.get("text", "").strip()
        args = args_text.split() if args_text else []
        base = self.settings.approved_directory
        current_dir = user_state.get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = base / target_name
            if not target_path.is_dir():
                await say(f"Directory not found: `{escape_mrkdwn(target_name)}`")
                return

            user_state["current_directory"] = target_path

            # Try to find a resumable session
            claude_integration = self.deps.get("claude_integration")
            session_id = None
            if claude_integration:
                existing = await claude_integration._find_resumable_session(
                    user_id, target_path
                )
                if existing:
                    session_id = existing.session_id
            user_state["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " - session resumed" if session_id else ""

            await say(
                f"Switched to `{escape_mrkdwn(target_name)}/`"
                f"{git_badge}{session_badge}"
            )
            return

        # No args -- list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError as e:
            await say(f"Error reading workspace: {e}")
            return

        if not entries:
            await say(
                f"No repos in `{escape_mrkdwn(str(base))}`.\n"
                'Clone one by telling me, e.g. _"clone org/repo"_.'
            )
            return

        lines: List[str] = []
        button_elements: List[Dict[str, Any]] = []
        current_name = current_dir.name if current_dir != base else None

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = ":package:" if is_git else ":file_folder:"
            marker = " :arrow_left:" if d.name == current_name else ""
            lines.append(f"{icon} `{escape_mrkdwn(d.name)}/`{marker}")

        # Build Block Kit buttons (max 5 per actions block, so chunk them)
        action_blocks: List[Dict[str, Any]] = []
        for i in range(0, len(entries), 5):
            chunk = entries[i : i + 5]
            elements = []
            for d in chunk:
                elements.append(
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": d.name},
                        "action_id": f"cd_{d.name}",
                        "value": d.name,
                    }
                )
            action_blocks.append({"type": "actions", "elements": elements})

        blocks: List[Dict[str, Any]] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Repos*\n\n" + "\n".join(lines),
                },
            },
            *action_blocks,
        ]

        await say(
            text="Repos: " + ", ".join(d.name for d in entries),
            blocks=blocks,
        )

    async def agentic_sync_channels(
        self,
        ack: Callable,
        say: Callable,
        command: Dict[str, Any],
        client: AsyncWebClient,
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Sync project channels: /sync_channels."""
        await ack()
        manager = self.deps.get("project_channels_manager")
        if not manager:
            await say("Project channel mode is not configured.")
            return

        await say("Syncing project channels...")

        try:
            result = await manager.sync_channels(client)
            await say(
                f"*Channel sync complete*\n"
                f"Created: {result.created} | Reused: {result.reused} | "
                f"Failed: {result.failed} | Deactivated: {result.deactivated}"
            )
        except Exception as e:
            logger.error("Channel sync failed", error=str(e))
            await say(f"Channel sync failed: {escape_mrkdwn(str(e))}")

    async def _agentic_callback(
        self,
        ack: Callable,
        body: Dict[str, Any],
        say: Callable,
        action: Dict[str, Any],
        client: AsyncWebClient,
        context: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Handle cd_ actions -- switch directory and resume session if available."""
        await ack()

        project_name = action.get("value", "")
        user_id = body.get("user", {}).get("id", "")
        user_state = context.get("user_state", {})

        if not project_name or not user_id:
            return

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await say(f"Directory not found: `{escape_mrkdwn(project_name)}`")
            return

        user_state["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = self.deps.get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                user_id, new_path
            )
            if existing:
                session_id = existing.session_id
        user_state["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " - session resumed" if session_id else ""

        # Update the original message to show the selection
        try:
            message_ts = body.get("message", {}).get("ts", "")
            channel_id = body.get("channel", {}).get("id", "")
            if message_ts and channel_id:
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=(
                        f"Switched to `{escape_mrkdwn(project_name)}/`"
                        f"{git_badge}{session_badge}"
                    ),
                    blocks=[],  # Remove buttons after selection
                )
            else:
                await say(
                    f"Switched to `{escape_mrkdwn(project_name)}/`"
                    f"{git_badge}{session_badge}"
                )
        except Exception:
            await say(
                f"Switched to `{escape_mrkdwn(project_name)}/`"
                f"{git_badge}{session_badge}"
            )

        # Audit log
        audit_logger = self.deps.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="cd",
                args=[project_name],
                success=True,
            )

"""In-process MCP tools for the Slack bot.

Registers SlackFileUpload, SlackReaction, ScheduleJob, ListScheduledJobs,
and RemoveScheduledJob as real SDK MCP tools so Claude discovers them
natively (no system-prompt hacking or permission-deny interception).
"""

from typing import Any, Callable, Dict, Optional

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool


def create_bot_mcp_server(
    file_upload_fn: Optional[Callable] = None,
    scheduler_fn: Optional[Callable] = None,
    reaction_fn: Optional[Callable] = None,
) -> McpSdkServerConfig:
    """Build an in-process MCP server with the bot's custom tools.

    Args:
        file_upload_fn: async (tool_input: dict) -> str
        scheduler_fn:   async (tool_name: str, tool_input: dict) -> str
        reaction_fn:    async (tool_input: dict) -> str
    """
    tools: list[SdkMcpTool[Any]] = []

    # ── SlackFileUpload ──────────────────────────────────────────────
    if file_upload_fn:

        @tool(
            "SlackFileUpload",
            "Upload a file or image to the current Slack channel. "
            "Use this whenever you need to send a file to the user.",
            {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to upload (absolute or relative to working directory)",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Display name for the file in Slack (optional, defaults to file name)",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title shown above the file in Slack (optional)",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Message posted alongside the file (optional)",
                    },
                },
                "required": ["file_path"],
            },
        )
        async def slack_file_upload(args: Dict[str, Any]) -> Dict[str, Any]:
            result = await file_upload_fn(args)
            is_error = result.startswith("Error")
            return {
                "content": [{"type": "text", "text": result}],
                "is_error": is_error,
            }

        tools.append(slack_file_upload)

    # ── SlackReaction ─────────────────────────────────────────────────
    if reaction_fn:

        @tool(
            "SlackReaction",
            "React to the user's Slack message with an emoji — like a human "
            "coworker would. Use sparingly and naturally: to acknowledge a "
            "request, celebrate, or show empathy. Don't react to every message.",
            {
                "type": "object",
                "properties": {
                    "emoji_name": {
                        "type": "string",
                        "description": (
                            "Emoji name without colons (e.g. 'thumbsup', 'eyes', "
                            "'tada', 'fire', 'heart', 'thinking_face', 'white_check_mark')"
                        ),
                    },
                    "remove": {
                        "type": "boolean",
                        "description": "Set true to remove a reaction instead of adding",
                    },
                },
                "required": ["emoji_name"],
            },
        )
        async def slack_reaction(args: Dict[str, Any]) -> Dict[str, Any]:
            result = await reaction_fn(args)
            is_error = result.startswith("Error")
            return {
                "content": [{"type": "text", "text": result}],
                "is_error": is_error,
            }

        tools.append(slack_reaction)

    # ── Scheduler tools ──────────────────────────────────────────────
    if scheduler_fn:

        @tool(
            "ScheduleJob",
            "Schedule a recurring cron job that runs a prompt on a schedule.",
            {
                "type": "object",
                "properties": {
                    "job_name": {
                        "type": "string",
                        "description": "Human-readable name for the job",
                    },
                    "cron_expression": {
                        "type": "string",
                        "description": (
                            "Standard 5-field crontab schedule. "
                            "Day-of-week: 0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat "
                            "(or named: SUN,MON,TUE,WED,THU,FRI,SAT). "
                            "Examples: '0 9 * * 1-5' weekdays 9am, "
                            "'0 10 * * 3' Wednesday 10am, "
                            "'*/30 * * * *' every 30min"
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The prompt to send to Claude when the job fires",
                    },
                    "skill_name": {
                        "type": "string",
                        "description": "Optional skill to invoke (e.g. 'commit')",
                    },
                },
                "required": ["job_name", "cron_expression", "prompt"],
            },
        )
        async def schedule_job(args: Dict[str, Any]) -> Dict[str, Any]:
            result = await scheduler_fn("ScheduleJob", args)
            is_error = result.startswith("Error")
            return {
                "content": [{"type": "text", "text": result}],
                "is_error": is_error,
            }

        tools.append(schedule_job)

        @tool(
            "ListScheduledJobs",
            "List all active scheduled jobs.",
            {"type": "object", "properties": {}},
        )
        async def list_scheduled_jobs(args: Dict[str, Any]) -> Dict[str, Any]:
            result = await scheduler_fn("ListScheduledJobs", args)
            return {"content": [{"type": "text", "text": result}]}

        tools.append(list_scheduled_jobs)

        @tool(
            "RemoveScheduledJob",
            "Remove a scheduled job by its ID.",
            {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The ID of the job to remove",
                    },
                },
                "required": ["job_id"],
            },
        )
        async def remove_scheduled_job(args: Dict[str, Any]) -> Dict[str, Any]:
            result = await scheduler_fn("RemoveScheduledJob", args)
            return {"content": [{"type": "text", "text": result}]}

        tools.append(remove_scheduled_job)

    return create_sdk_mcp_server(
        name="slack-bot-tools",
        version="1.0.0",
        tools=tools,
    )

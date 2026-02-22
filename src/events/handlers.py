"""Event handlers that bridge the event bus to Claude and Slack.

AgentHandler: translates events into ClaudeIntegration.run_command() calls.
"""

from pathlib import Path
from typing import Any, Dict, List

import structlog

from ..claude.facade import ClaudeIntegration
from .bus import Event, EventBus
from .types import AgentResponseEvent, ScheduledEvent, WebhookEvent

logger = structlog.get_logger()


class AgentHandler:
    """Translates incoming events into Claude agent executions.

    Webhook events are converted into prompts and sent to
    ClaudeIntegration.run_command(). Scheduled events are routed
    through the orchestrator so they share the channel's session.
    """

    def __init__(
        self,
        event_bus: EventBus,
        claude_integration: ClaudeIntegration,
        default_working_directory: Path,
        default_user_id: str = "",
        slack_client: Any = None,
        orchestrator: Any = None,
    ) -> None:
        self.event_bus = event_bus
        self.claude = claude_integration
        self.default_working_directory = default_working_directory
        self.default_user_id = default_user_id
        self.slack_client = slack_client
        self.orchestrator = orchestrator

    def register(self) -> None:
        """Subscribe to events that need agent processing."""
        self.event_bus.subscribe(WebhookEvent, self.handle_webhook)
        self.event_bus.subscribe(ScheduledEvent, self.handle_scheduled)

    async def handle_webhook(self, event: Event) -> None:
        """Process a webhook event through Claude."""
        if not isinstance(event, WebhookEvent):
            return

        logger.info(
            "Processing webhook event through agent",
            provider=event.provider,
            event_type=event.event_type_name,
            delivery_id=event.delivery_id,
        )

        prompt = self._build_webhook_prompt(event)

        try:
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=self.default_working_directory,
                user_id=self.default_user_id,
            )

            if response.content:
                # Publish with empty channel_id — the NotificationService
                # will broadcast to configured notification_channel_ids.
                await self.event_bus.publish(
                    AgentResponseEvent(
                        channel_id="",
                        text=response.content,
                        originating_event_id=event.id,
                    )
                )
        except Exception:
            logger.exception(
                "Agent execution failed for webhook event",
                provider=event.provider,
                event_id=event.id,
            )

    async def handle_scheduled(self, event: Event) -> None:
        """Process a scheduled event by routing through the orchestrator.

        This ensures the job shares the same Claude session as the channel's
        conversation, so the user and job have full context of each other.
        Falls back to standalone run_command if orchestrator is unavailable.
        """
        if not isinstance(event, ScheduledEvent):
            return

        logger.info(
            "Processing scheduled event through agent",
            job_id=event.job_id,
            job_name=event.job_name,
        )

        prompt = event.prompt
        if event.skill_name:
            prompt = (
                f"/{event.skill_name}\n\n{prompt}" if prompt else f"/{event.skill_name}"
            )

        # Route through orchestrator if available — this shares the channel's
        # session so the job and user conversation have mutual context.
        if self.orchestrator and self.slack_client and event.target_channel_ids:
            for channel_id in event.target_channel_ids:
                try:
                    await self.orchestrator.run_scheduled_prompt(
                        prompt=prompt,
                        channel_id=channel_id,
                        user_id=self.default_user_id,
                        client=self.slack_client,
                    )
                except Exception:
                    logger.exception(
                        "Orchestrator scheduled execution failed",
                        job_name=event.job_name,
                        channel_id=channel_id,
                    )
            return

        # Fallback: standalone execution (no shared session context)
        working_dir = event.working_directory or self.default_working_directory

        try:
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=working_dir,
                user_id=self.default_user_id,
            )

            if response.content:
                for channel_id in event.target_channel_ids:
                    await self.event_bus.publish(
                        AgentResponseEvent(
                            channel_id=channel_id,
                            text=response.content,
                            originating_event_id=event.id,
                        )
                    )

                if not event.target_channel_ids:
                    await self.event_bus.publish(
                        AgentResponseEvent(
                            channel_id="",
                            text=response.content,
                            originating_event_id=event.id,
                        )
                    )
        except Exception:
            logger.exception(
                "Agent execution failed for scheduled event",
                job_id=event.job_id,
                event_id=event.id,
            )

    def _build_webhook_prompt(self, event: WebhookEvent) -> str:
        """Build a Claude prompt from a webhook event."""
        payload_summary = self._summarize_payload(event.payload)

        return (
            f"A {event.provider} webhook event occurred.\n"
            f"Event type: {event.event_type_name}\n"
            f"Payload summary:\n{payload_summary}\n\n"
            f"Analyze this event and provide a concise summary. "
            f"Highlight anything that needs my attention."
        )

    def _summarize_payload(self, payload: Dict[str, Any], max_depth: int = 2) -> str:
        """Create a readable summary of a webhook payload."""
        lines: List[str] = []
        self._flatten_dict(payload, lines, max_depth=max_depth)
        summary = "\n".join(lines)
        if len(summary) > 2000:
            summary = summary[:2000] + "\n... (truncated)"
        return summary

    def _flatten_dict(
        self,
        data: Any,
        lines: list,
        prefix: str = "",
        depth: int = 0,
        max_depth: int = 2,
    ) -> None:
        """Flatten a nested dict into key: value lines."""
        if depth >= max_depth:
            lines.append(f"{prefix}: ...")
            return

        if isinstance(data, dict):
            for key, value in data.items():
                full_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, (dict, list)):
                    self._flatten_dict(value, lines, full_key, depth + 1, max_depth)
                else:
                    val_str = str(value)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "..."
                    lines.append(f"{full_key}: {val_str}")
        elif isinstance(data, list):
            lines.append(f"{prefix}: [{len(data)} items]")
            for i, item in enumerate(data[:3]):
                self._flatten_dict(item, lines, f"{prefix}[{i}]", depth + 1, max_depth)
        else:
            lines.append(f"{prefix}: {data}")

"""Notification service for delivering proactive agent responses to Slack.

Subscribes to AgentResponseEvent on the event bus and delivers messages
through the Slack Web API with rate limiting (~1 msg/sec per channel).
"""

import asyncio
from typing import List, Optional

import structlog
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from ..events.bus import Event, EventBus
from ..events.types import AgentResponseEvent

logger = structlog.get_logger()

# Slack rate limit: ~1 msg/sec per channel for chat.postMessage
SEND_INTERVAL_SECONDS = 1.1


class NotificationService:
    """Delivers agent responses to Slack channels with rate limiting."""

    def __init__(
        self,
        event_bus: EventBus,
        client: AsyncWebClient,
        default_channel_ids: Optional[List[str]] = None,
    ) -> None:
        self.event_bus = event_bus
        self.client = client
        self.default_channel_ids = default_channel_ids or []
        self._send_queue: asyncio.Queue[AgentResponseEvent] = asyncio.Queue()
        self._last_send_per_channel: dict[str, float] = {}
        self._running = False
        self._sender_task: Optional[asyncio.Task[None]] = None

    def register(self) -> None:
        """Subscribe to agent response events."""
        self.event_bus.subscribe(AgentResponseEvent, self.handle_response)

    async def start(self) -> None:
        """Start the send queue processor."""
        if self._running:
            return
        self._running = True
        self._sender_task = asyncio.create_task(self._process_send_queue())
        logger.info("Notification service started")

    async def stop(self) -> None:
        """Stop the send queue processor."""
        if not self._running:
            return
        self._running = False
        if self._sender_task:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
        logger.info("Notification service stopped")

    async def handle_response(self, event: Event) -> None:
        """Queue an agent response for delivery."""
        if not isinstance(event, AgentResponseEvent):
            return
        await self._send_queue.put(event)

    async def _process_send_queue(self) -> None:
        """Process queued messages with rate limiting."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            channel_ids = self._resolve_channel_ids(event)
            for channel_id in channel_ids:
                await self._rate_limited_send(channel_id, event)

    def _resolve_channel_ids(self, event: AgentResponseEvent) -> List[str]:
        """Determine which channels to send to."""
        if event.channel_id:
            return [event.channel_id]
        return list(self.default_channel_ids)

    async def _rate_limited_send(
        self, channel_id: str, event: AgentResponseEvent
    ) -> None:
        """Send message with per-channel rate limiting."""
        loop = asyncio.get_event_loop()
        now = loop.time()
        last_send = self._last_send_per_channel.get(channel_id, 0.0)
        wait_time = SEND_INTERVAL_SECONDS - (now - last_send)

        if wait_time > 0:
            await asyncio.sleep(wait_time)

        try:
            text = event.text
            chunks = self._split_message(text)

            for chunk in chunks:
                kwargs = {
                    "channel": channel_id,
                    "text": chunk,
                }
                if event.thread_ts:
                    kwargs["thread_ts"] = event.thread_ts

                await self.client.chat_postMessage(**kwargs)
                self._last_send_per_channel[channel_id] = (
                    asyncio.get_event_loop().time()
                )

                if len(chunks) > 1:
                    await asyncio.sleep(SEND_INTERVAL_SECONDS)

            logger.info(
                "Notification sent",
                channel_id=channel_id,
                text_length=len(text),
                chunks=len(chunks),
                originating_event=event.originating_event_id,
            )
        except SlackApiError as e:
            logger.error(
                "Failed to send notification",
                channel_id=channel_id,
                error=str(e),
                event_id=event.id,
            )

    def _split_message(self, text: str, max_length: int = 3900) -> List[str]:
        """Split long messages at paragraph boundaries."""
        if len(text) <= max_length:
            return [text]

        chunks: List[str] = []
        while text:
            if len(text) <= max_length:
                chunks.append(text)
                break

            split_pos = text.rfind("\n\n", 0, max_length)
            if split_pos == -1:
                split_pos = text.rfind("\n", 0, max_length)
            if split_pos == -1:
                split_pos = text.rfind(" ", 0, max_length)
            if split_pos == -1:
                split_pos = max_length

            chunks.append(text[:split_pos])
            text = text[split_pos:].lstrip()

        return chunks

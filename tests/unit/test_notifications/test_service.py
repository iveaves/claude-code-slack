"""Tests for the notification service."""

from unittest.mock import AsyncMock

import pytest

from src.events.bus import Event, EventBus
from src.events.types import AgentResponseEvent
from src.notifications.service import NotificationService


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


@pytest.fixture
def service(event_bus: EventBus, mock_client: AsyncMock) -> NotificationService:
    svc = NotificationService(
        event_bus=event_bus,
        client=mock_client,
        default_channel_ids=["C001", "C002"],
    )
    svc.register()
    return svc


class TestNotificationService:
    """Tests for NotificationService."""

    async def test_handle_response_queues_event(
        self, service: NotificationService
    ) -> None:
        """Events are queued for delivery."""
        event = AgentResponseEvent(channel_id="C001", text="hello")
        await service.handle_response(event)
        assert service._send_queue.qsize() == 1

    async def test_resolve_channel_ids_specific(
        self, service: NotificationService
    ) -> None:
        """Specific channel_id takes precedence over defaults."""
        event = AgentResponseEvent(channel_id="C999", text="test")
        ids = service._resolve_channel_ids(event)
        assert ids == ["C999"]

    async def test_resolve_channel_ids_default(
        self, service: NotificationService
    ) -> None:
        """Empty channel_id falls back to default channel IDs."""
        event = AgentResponseEvent(channel_id="", text="test")
        ids = service._resolve_channel_ids(event)
        assert ids == ["C001", "C002"]

    def test_split_message_short(self, service: NotificationService) -> None:
        """Short messages are not split."""
        chunks = service._split_message("short text")
        assert len(chunks) == 1
        assert chunks[0] == "short text"

    def test_split_message_long(self, service: NotificationService) -> None:
        """Long messages are split at boundaries."""
        text = "A" * 3800 + "\n\n" + "B" * 200
        chunks = service._split_message(text, max_length=3900)
        assert len(chunks) >= 1
        total_len = sum(len(c) for c in chunks)
        assert total_len > 0

    def test_split_message_no_boundary(self, service: NotificationService) -> None:
        """Messages without boundaries are hard-split."""
        text = "A" * 5000
        chunks = service._split_message(text, max_length=3900)
        assert len(chunks) == 2
        assert len(chunks[0]) == 3900
        assert len(chunks[1]) == 1100

    async def test_send_to_slack(
        self, service: NotificationService, mock_client: AsyncMock
    ) -> None:
        """Messages are sent via the Slack client."""
        event = AgentResponseEvent(channel_id="C123", text="hello world")
        await service._rate_limited_send("C123", event)

        mock_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["text"] == "hello world"

    async def test_ignores_non_response_events(
        self, service: NotificationService
    ) -> None:
        """Non-AgentResponseEvent events are ignored."""
        event = Event(source="test")
        await service.handle_response(event)
        assert service._send_queue.qsize() == 0

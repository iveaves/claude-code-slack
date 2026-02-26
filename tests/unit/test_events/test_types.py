"""Tests for event types."""

from pathlib import Path

from src.events.types import (
    AgentResponseEvent,
    ScheduledEvent,
    UserMessageEvent,
    WebhookEvent,
)


class TestEventTypes:
    """Tests for concrete event dataclasses."""

    def test_user_message_event_defaults(self) -> None:
        event = UserMessageEvent(user_id="U01ABC", channel_id="C01XYZ", text="hello")
        assert event.source == "slack"
        assert event.user_id == "U01ABC"
        assert event.channel_id == "C01XYZ"
        assert event.text == "hello"
        assert event.event_type == "UserMessageEvent"

    def test_webhook_event_defaults(self) -> None:
        event = WebhookEvent(
            provider="github",
            event_type_name="push",
            payload={"ref": "refs/heads/main"},
            delivery_id="abc-123",
        )
        assert event.source == "webhook"
        assert event.provider == "github"
        assert event.payload["ref"] == "refs/heads/main"

    def test_scheduled_event_defaults(self) -> None:
        event = ScheduledEvent(
            job_id="j1",
            job_name="daily-standup",
            prompt="Generate standup",
            target_channel_ids=["C001", "C002"],
        )
        assert event.source == "scheduler"
        assert event.target_channel_ids == ["C001", "C002"]

    def test_agent_response_event(self) -> None:
        event = AgentResponseEvent(
            channel_id="C789",
            text="Here's your summary",
            originating_event_id="orig-1",
        )
        assert event.source == "agent"
        assert event.channel_id == "C789"
        assert event.originating_event_id == "orig-1"

    def test_scheduled_event_with_skill(self) -> None:
        event = ScheduledEvent(
            job_name="standup",
            prompt="",
            skill_name="daily-standup",
            working_directory=Path("/projects/myapp"),
        )
        assert event.skill_name == "daily-standup"
        assert event.working_directory == Path("/projects/myapp")

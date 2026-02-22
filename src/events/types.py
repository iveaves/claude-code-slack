"""Concrete event types for the event bus."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bus import Event


@dataclass
class UserMessageEvent(Event):
    """A message from a Slack user."""

    user_id: str = ""
    channel_id: str = ""
    text: str = ""
    working_directory: Path = field(default_factory=lambda: Path("."))
    source: str = "slack"


@dataclass
class WebhookEvent(Event):
    """An external webhook delivery (GitHub, Notion, etc.)."""

    provider: str = ""
    event_type_name: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    delivery_id: str = ""
    source: str = "webhook"


@dataclass
class ScheduledEvent(Event):
    """A cron/scheduled trigger."""

    job_id: str = ""
    job_name: str = ""
    prompt: str = ""
    working_directory: Path = field(default_factory=lambda: Path("."))
    target_channel_ids: List[str] = field(default_factory=list)
    skill_name: Optional[str] = None
    source: str = "scheduler"


@dataclass
class AgentResponseEvent(Event):
    """An agent has produced a response to deliver."""

    channel_id: str = ""
    text: str = ""
    thread_ts: Optional[str] = None
    source: str = "agent"
    originating_event_id: Optional[str] = None

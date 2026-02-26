"""Tests for the Slack MessageOrchestrator.

Validates agentic mode routing, command registration, callback handling,
and the stream/reaction/scheduler callback factories.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_bolt.app.async_app import AsyncApp

from src.bot.orchestrator import MessageOrchestrator
from src.config.settings import Settings


def _make_settings(tmp_path, **overrides):
    defaults = {
        "_env_file": None,
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
        "approved_directory": str(tmp_path),
        "agentic_mode": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def settings(tmp_path):
    return _make_settings(tmp_path)


@pytest.fixture
def orchestrator(settings):
    deps = {
        "auth_manager": MagicMock(),
        "security_validator": MagicMock(),
        "rate_limiter": None,
        "audit_logger": MagicMock(),
        "claude_integration": AsyncMock(),
        "storage": AsyncMock(),
        "event_bus": MagicMock(),
        "project_registry": None,
        "project_channels_manager": None,
        "scheduler": None,
    }
    return MessageOrchestrator(settings, deps)


class TestOrchestratorInit:
    """Test orchestrator initialization and handler registration."""

    def test_creates_with_settings(self, orchestrator):
        assert orchestrator.settings is not None
        assert orchestrator.deps is not None

    def test_registers_agentic_handlers(self, orchestrator):
        """Agentic mode registers slash commands on the Slack app."""
        app = MagicMock(spec=AsyncApp)
        orchestrator.register_handlers(app)
        # Should register /new, /status, /verbose, /repo at minimum
        assert app.command.call_count >= 4

    def test_registers_message_handler(self, orchestrator):
        """Registers a message event listener."""
        app = MagicMock(spec=AsyncApp)
        orchestrator.register_handlers(app)
        # Should register at least one app.event("message") handler
        assert app.event.called


class TestReactionCallback:
    """Test the reaction callback factory."""

    def test_creates_callback(self, orchestrator):
        client = AsyncMock()
        cb = orchestrator._make_reaction_callback("C01CH", "1234.5678", client)
        assert callable(cb)

    async def test_adds_reaction(self, orchestrator):
        client = AsyncMock()
        client.reactions_add = AsyncMock()
        cb = orchestrator._make_reaction_callback("C01CH", "1234.5678", client)

        result = await cb({"emoji_name": "thumbsup"})

        client.reactions_add.assert_called_once_with(
            name="thumbsup", channel="C01CH", timestamp="1234.5678"
        )
        assert "Added" in result

    async def test_removes_reaction(self, orchestrator):
        client = AsyncMock()
        client.reactions_remove = AsyncMock()
        cb = orchestrator._make_reaction_callback("C01CH", "1234.5678", client)

        result = await cb({"emoji_name": "thumbsup", "remove": True})

        client.reactions_remove.assert_called_once()
        assert "Removed" in result

    async def test_strips_colons(self, orchestrator):
        client = AsyncMock()
        client.reactions_add = AsyncMock()
        cb = orchestrator._make_reaction_callback("C01CH", "1234.5678", client)

        await cb({"emoji_name": ":fire:"})

        client.reactions_add.assert_called_once_with(
            name="fire", channel="C01CH", timestamp="1234.5678"
        )

    async def test_handles_already_reacted(self, orchestrator):
        client = AsyncMock()
        client.reactions_add = AsyncMock(side_effect=Exception("already_reacted"))
        cb = orchestrator._make_reaction_callback("C01CH", "1234.5678", client)

        result = await cb({"emoji_name": "thumbsup"})
        assert "Already" in result

    async def test_empty_emoji_returns_error(self, orchestrator):
        client = AsyncMock()
        cb = orchestrator._make_reaction_callback("C01CH", "1234.5678", client)

        result = await cb({"emoji_name": ""})
        assert "Error" in result


class TestFileUploadCallback:
    """Test the file upload callback factory."""

    async def test_missing_file_path(self, orchestrator):
        client = AsyncMock()
        cb = orchestrator._make_file_upload_callback("C01CH", "U01USER", client)
        result = await cb({"file_path": ""})
        assert "Error" in result

    async def test_nonexistent_file(self, orchestrator):
        client = AsyncMock()
        cb = orchestrator._make_file_upload_callback("C01CH", "U01USER", client)
        result = await cb({"file_path": "/nonexistent/file.txt"})
        assert "Error" in result


class TestSchedulerCallback:
    """Test the scheduler callback factory."""

    async def test_unknown_tool(self, orchestrator):
        orchestrator.deps["scheduler"] = AsyncMock()
        cb = orchestrator._make_scheduler_callback(
            "C01CH", "U01USER", working_directory="/tmp"
        )
        result = await cb("UnknownTool", {})
        assert "Unknown" in result

    def test_no_scheduler_returns_none(self, orchestrator):
        """When scheduler is not in deps, callback factory returns None."""
        orchestrator.deps["scheduler"] = None
        cb = orchestrator._make_scheduler_callback(
            "C01CH", "U01USER", working_directory="/tmp"
        )
        assert cb is None

"""Tests for Slack message formatting.

Verifies that ResponseFormatter correctly formats Claude responses
for Slack's mrkdwn format, splits long messages, and handles
code blocks and special characters.
"""

import pytest

from src.bot.utils.formatting import FormattedMessage, ResponseFormatter
from src.bot.utils.slack_format import escape_mrkdwn
from src.config.settings import Settings


def _make_settings(tmp_path, **overrides):
    defaults = {
        "_env_file": None,
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
        "approved_directory": str(tmp_path),
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestFormattedMessage:
    """Test FormattedMessage dataclass."""

    def test_creation_with_text(self):
        msg = FormattedMessage("hello world")
        assert msg.text == "hello world"

    def test_empty_text(self):
        msg = FormattedMessage("")
        assert msg.text == ""


class TestResponseFormatter:
    """Test ResponseFormatter for Slack output."""

    @pytest.fixture
    def formatter(self, tmp_path):
        settings = _make_settings(tmp_path)
        return ResponseFormatter(settings)

    def test_format_simple_message(self, formatter):
        result = formatter.format_claude_response("Hello, world!")
        assert len(result) >= 1
        assert result[0].text == "Hello, world!"

    def test_format_empty_message(self, formatter):
        result = formatter.format_claude_response("")
        assert len(result) >= 1

    def test_format_preserves_code_blocks(self, formatter):
        text = "Here's code:\n```python\nprint('hello')\n```\nDone."
        result = formatter.format_claude_response(text)
        combined = "".join(m.text for m in result)
        assert "```" in combined
        assert "print" in combined

    def test_format_long_message_splits(self, formatter):
        """Messages over Slack's limit are split into chunks."""
        long_text = "A" * 5000
        result = formatter.format_claude_response(long_text)
        assert len(result) >= 2
        # All content preserved
        combined = "".join(m.text for m in result)
        assert len(combined) >= 5000


class TestSlackMrkdwnEscape:
    """Test Slack mrkdwn escaping utility."""

    def test_escapes_ampersand(self):
        assert "&amp;" in escape_mrkdwn("a & b")

    def test_escapes_angle_brackets(self):
        result = escape_mrkdwn("<script>")
        assert "<" not in result or "&lt;" in result

    def test_preserves_normal_text(self):
        assert escape_mrkdwn("hello world") == "hello world"

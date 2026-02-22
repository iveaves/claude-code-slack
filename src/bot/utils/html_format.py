"""Backwards-compatibility shim -- delegates to slack_format.

During the migration from Telegram HTML to Slack mrkdwn, other modules
that still ``from ..utils.html_format import escape_html`` will
transparently receive the Slack-compatible escaping function.  The
escaping logic is identical (& < > entities), so existing call-sites
continue to work.

The ``markdown_to_telegram_html`` name is re-exported as an alias for
``markdown_to_slack_mrkdwn`` so callers that have not been updated yet
still resolve at import time.
"""

from .slack_format import escape_mrkdwn as escape_html  # noqa: F401
from .slack_format import markdown_to_slack_mrkdwn as markdown_to_telegram_html  # noqa: F401

__all__ = ["escape_html", "markdown_to_telegram_html"]

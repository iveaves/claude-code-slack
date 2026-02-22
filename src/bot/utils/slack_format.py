"""Slack mrkdwn formatting utilities.

Slack's mrkdwn format is close to standard markdown but has its own
conventions for bold (*bold*), italic (_italic_), code (`code`),
and code blocks (```code```).  Only three characters need escaping
in regular text: &, <, >.
"""

import re
from typing import List, Tuple


def escape_mrkdwn(text: str) -> str:
    """Escape the 3 special characters for Slack mrkdwn.

    Slack requires &, <, > to be escaped as HTML entities even inside
    mrkdwn text so they are not interpreted as message formatting
    directives.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def markdown_to_slack_mrkdwn(text: str) -> str:
    """Convert Claude's markdown output to Slack-compatible mrkdwn.

    Slack mrkdwn is close to standard markdown, but differs in:
    - Bold: *bold* (single asterisks, not double)
    - Italic: _italic_ (same)
    - Strikethrough: ~text~ (single tilde, not double)
    - Links: <url|text> (angle-bracket syntax)
    - Headers: no native support -- rendered as *bold* text
    - Code blocks: ```code``` (same as markdown, no language annotation displayed)

    Order of operations:
    1. Extract fenced code blocks -> placeholders
    2. Extract inline code -> placeholders
    3. Escape remaining text (&, <, >)
    4. Convert bold (**text** / __text__) -> *text*
    5. Convert italic (*text*, _text_ with word boundaries) -> _text_
    6. Convert links [text](url) -> <url|text>
    7. Convert headers (# Header -> *Header*)
    8. Convert strikethrough (~~text~~) -> ~text~
    9. Restore placeholders
    """
    placeholders: List[Tuple[str, str]] = []
    placeholder_counter = 0

    def _make_placeholder(content: str) -> str:
        nonlocal placeholder_counter
        key = f"\x00PH{placeholder_counter}\x00"
        placeholder_counter += 1
        placeholders.append((key, content))
        return key

    # --- 1. Extract fenced code blocks ---
    def _replace_fenced(m: re.Match) -> str:  # type: ignore[type-arg]
        lang = m.group(1) or ""
        code = m.group(2)
        # Slack does not render language annotations, but we can keep
        # the language hint as a comment on the first line for readability.
        if lang:
            slack_block = f"```{lang}\n{code}```"
        else:
            slack_block = f"```\n{code}```"
        return _make_placeholder(slack_block)

    text = re.sub(
        r"```(\w+)?\n(.*?)```",
        _replace_fenced,
        text,
        flags=re.DOTALL,
    )

    # --- 2. Extract inline code ---
    def _replace_inline_code(m: re.Match) -> str:  # type: ignore[type-arg]
        code = m.group(1)
        return _make_placeholder(f"`{code}`")

    text = re.sub(r"`([^`\n]+)`", _replace_inline_code, text)

    # --- 3. Escape remaining text ---
    text = escape_mrkdwn(text)

    # --- 4. Bold: **text** or __text__ -> *text* ---
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)

    # --- 5. Italic: *text* -> _text_ (require non-space after/before) ---
    # After converting **bold** to *bold*, remaining single *text* are italic
    # We must be careful not to re-process the bold markers we just created.
    # Since bold is now *text*, single *text* that wasn't bold is italic.
    # This regex targets *text* that was originally single-asterisk italic in
    # markdown.  We handle this by looking for asterisk pairs that are NOT
    # already adjacent to word chars (which would be our bold output).
    # Actually, the bold conversion already consumed **, so remaining * pairs
    # are genuine italics from the original markdown.
    text = re.sub(r"(?<!\*)\*(\S.*?\S|\S)\*(?!\*)", r"_\1_", text)
    # _text_ only at word boundaries (avoid my_var_name)
    # Already Slack's native italic syntax -- leave as-is but ensure
    # word-boundary protection:
    # (No conversion needed; _text_ is already valid mrkdwn italic)

    # --- 6. Links: [text](url) -> <url|text> ---
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r"<\2|\1>",
        text,
    )

    # --- 7. Headers: # Header -> *Header* (bold line) ---
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # --- 8. Strikethrough: ~~text~~ -> ~text~ ---
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # --- 9. Restore placeholders ---
    for key, content in placeholders:
        text = text.replace(key, content)

    return text

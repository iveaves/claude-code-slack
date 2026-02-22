"""Format bot responses for optimal display in Slack."""

import re
from dataclasses import dataclass, field
from typing import List, Optional

from ...config.settings import Settings
from .slack_format import escape_mrkdwn, markdown_to_slack_mrkdwn


@dataclass
class FormattedMessage:
    """Represents a formatted message for Slack.

    Slack always uses mrkdwn for text formatting, so there is no
    ``parse_mode`` field.  Interactive elements (buttons, menus) are
    expressed via Block Kit *actions* blocks attached alongside the
    message text.
    """

    text: str
    blocks: Optional[List[dict]] = field(default=None)

    def __len__(self) -> int:
        """Return length of message text."""
        return len(self.text)


class ResponseFormatter:
    """Format Claude responses for Slack display."""

    def __init__(self, settings: Settings):
        """Initialize formatter with settings."""
        self.settings = settings
        self.max_message_length = 3900  # Slack limit is 4000, leave some buffer
        self.max_code_block_length = (
            15000  # Max length for individual code blocks before splitting
        )

    def format_claude_response(
        self, text: str, context: Optional[dict] = None
    ) -> List[FormattedMessage]:
        """Enhanced formatting with context awareness and semantic chunking."""
        # Clean and prepare text
        text = self._clean_text(text)

        # Check if we need semantic chunking (for complex content)
        if self._should_use_semantic_chunking(text):
            # Use enhanced semantic chunking for complex content
            chunks = self._semantic_chunk(text, context)
            messages = []
            for chunk in chunks:
                formatted = self._format_chunk(chunk)
                messages.extend(formatted)
        else:
            # Use original simple formatting for basic content
            text = self._format_code_blocks(text)
            messages = self._split_message(text)

        # Add context-aware quick actions to the last message
        if messages and self.settings.enable_quick_actions:
            messages[-1].blocks = self._get_contextual_keyboard(context)

        return (
            messages
            if messages
            else [FormattedMessage("_(No content to display)_")]
        )

    def _should_use_semantic_chunking(self, text: str) -> bool:
        """Determine if semantic chunking is needed."""
        code_block_count = text.count("```")
        has_file_operations = any(
            indicator in text
            for indicator in [
                "Creating file",
                "Editing file",
                "Reading file",
                "Writing to",
                "Modified file",
                "Deleted file",
                "File created",
                "File updated",
            ]
        )
        is_very_long = len(text) > self.max_message_length * 2

        return code_block_count > 2 or has_file_operations or is_very_long

    def format_error_message(
        self, error: str, error_type: str = "Error"
    ) -> FormattedMessage:
        """Format error message with appropriate styling."""
        icon = {
            "Error": ":x:",
            "Warning": ":warning:",
            "Info": ":information_source:",
            "Security": ":shield:",
            "Rate Limit": ":stopwatch:",
        }.get(error_type, ":x:")

        text = f"{icon} *{escape_mrkdwn(error_type)}*\n\n{escape_mrkdwn(error)}"

        return FormattedMessage(text)

    def format_success_message(
        self, message: str, title: str = "Success"
    ) -> FormattedMessage:
        """Format success message with appropriate styling."""
        text = f":white_check_mark: *{escape_mrkdwn(title)}*\n\n{escape_mrkdwn(message)}"
        return FormattedMessage(text)

    def format_info_message(
        self, message: str, title: str = "Info"
    ) -> FormattedMessage:
        """Format info message with appropriate styling."""
        text = f":information_source: *{escape_mrkdwn(title)}*\n\n{escape_mrkdwn(message)}"
        return FormattedMessage(text)

    def format_code_output(
        self, output: str, language: str = "", title: str = "Output"
    ) -> List[FormattedMessage]:
        """Format code output with syntax highlighting."""
        if not output.strip():
            return [
                FormattedMessage(
                    f":page_facing_up: *{escape_mrkdwn(title)}*\n\n_(empty output)_"
                )
            ]

        # Check if the code block is too long
        if len(output) > self.max_code_block_length:
            output = (
                output[: self.max_code_block_length - 100]
                + "\n... (output truncated)"
            )

        if language:
            code_block = f"```{language}\n{output}```"
        else:
            code_block = f"```\n{output}```"

        text = f":page_facing_up: *{escape_mrkdwn(title)}*\n\n{code_block}"

        return self._split_message(text)

    def format_file_list(
        self, files: List[str], directory: str = ""
    ) -> FormattedMessage:
        """Format file listing with appropriate icons."""
        safe_dir = escape_mrkdwn(directory)
        if not files:
            text = f":open_file_folder: *{safe_dir}*\n\n_(empty directory)_"
        else:
            file_lines = []
            for f in files[:50]:  # Limit to 50 items
                safe_file = escape_mrkdwn(f)
                if f.endswith("/"):
                    file_lines.append(f":file_folder: {safe_file}")
                else:
                    file_lines.append(f":page_facing_up: {safe_file}")

            file_text = "\n".join(file_lines)
            if len(files) > 50:
                file_text += f"\n\n_... and {len(files) - 50} more items_"

            text = f":open_file_folder: *{safe_dir}*\n\n{file_text}"

        return FormattedMessage(text)

    def format_progress_message(
        self, message: str, percentage: Optional[float] = None
    ) -> FormattedMessage:
        """Format progress message with optional progress bar."""
        safe_msg = escape_mrkdwn(message)
        if percentage is not None:
            # Create simple progress bar
            filled = int(percentage / 10)
            empty = 10 - filled
            progress_bar = "\u2593" * filled + "\u2591" * empty
            text = f":arrows_counterclockwise: *{safe_msg}*\n\n{progress_bar} {percentage:.0f}%"
        else:
            text = f":arrows_counterclockwise: *{safe_msg}*"

        return FormattedMessage(text)

    def _semantic_chunk(self, text: str, context: Optional[dict]) -> List[dict]:
        """Split text into semantic chunks based on content type."""
        chunks = []

        # Identify different content sections
        sections = self._identify_sections(text)

        for section in sections:
            if section["type"] == "code_block":
                chunks.extend(self._chunk_code_block(section))
            elif section["type"] == "explanation":
                chunks.extend(self._chunk_explanation(section))
            elif section["type"] == "file_operations":
                chunks.append(self._format_file_operations_section(section))
            elif section["type"] == "mixed":
                chunks.extend(self._chunk_mixed_content(section))
            else:
                # Default text chunking
                chunks.extend(self._chunk_text(section))

        return chunks

    def _identify_sections(self, text: str) -> List[dict]:
        """Identify different content types in the text."""
        sections = []
        lines = text.split("\n")
        current_section: dict = {"type": "text", "content": "", "start_line": 0}
        in_code_block = False

        for i, line in enumerate(lines):
            # Check for code block markers
            if line.strip().startswith("```"):
                if not in_code_block:
                    # Start of code block
                    if current_section["content"].strip():
                        sections.append(current_section)
                    in_code_block = True
                    current_section = {
                        "type": "code_block",
                        "content": line + "\n",
                        "start_line": i,
                    }
                else:
                    # End of code block
                    current_section["content"] += line + "\n"
                    sections.append(current_section)
                    in_code_block = False
                    current_section = {
                        "type": "text",
                        "content": "",
                        "start_line": i + 1,
                    }
            elif in_code_block:
                current_section["content"] += line + "\n"
            else:
                # Check for file operation patterns
                if self._is_file_operation_line(line):
                    if current_section["type"] != "file_operations":
                        if current_section["content"].strip():
                            sections.append(current_section)
                        current_section = {
                            "type": "file_operations",
                            "content": line + "\n",
                            "start_line": i,
                        }
                    else:
                        current_section["content"] += line + "\n"
                else:
                    # Regular text
                    if current_section["type"] != "text":
                        if current_section["content"].strip():
                            sections.append(current_section)
                        current_section = {
                            "type": "text",
                            "content": line + "\n",
                            "start_line": i,
                        }
                    else:
                        current_section["content"] += line + "\n"

        # Add the last section
        if current_section["content"].strip():
            sections.append(current_section)

        return sections

    def _is_file_operation_line(self, line: str) -> bool:
        """Check if a line indicates file operations."""
        file_indicators = [
            "Creating file",
            "Editing file",
            "Reading file",
            "Writing to",
            "Modified file",
            "Deleted file",
            "File created",
            "File updated",
        ]
        return any(indicator in line for indicator in file_indicators)

    def _chunk_code_block(self, section: dict) -> List[dict]:
        """Handle code block chunking."""
        content = section["content"]
        if len(content) <= self.max_code_block_length:
            return [{"type": "code_block", "content": content, "format": "single"}]

        # Split large code blocks
        chunks = []
        lines = content.split("\n")
        current_chunk = lines[0] + "\n"  # Start with the ``` line

        for line in lines[1:-1]:  # Skip first and last ``` lines
            if len(current_chunk + line + "\n```\n") > self.max_code_block_length:
                current_chunk += "```"
                chunks.append(
                    {"type": "code_block", "content": current_chunk, "format": "split"}
                )
                current_chunk = "```\n" + line + "\n"
            else:
                current_chunk += line + "\n"

        current_chunk += lines[-1]  # Add the closing ```
        chunks.append(
            {"type": "code_block", "content": current_chunk, "format": "split"}
        )

        return chunks

    def _chunk_explanation(self, section: dict) -> List[dict]:
        """Handle explanation text chunking."""
        content = section["content"]
        if len(content) <= self.max_message_length:
            return [{"type": "explanation", "content": content}]

        # Split by paragraphs first
        paragraphs = content.split("\n\n")
        chunks = []
        current_chunk = ""

        for paragraph in paragraphs:
            if len(current_chunk + paragraph + "\n\n") > self.max_message_length:
                if current_chunk:
                    chunks.append(
                        {"type": "explanation", "content": current_chunk.strip()}
                    )
                current_chunk = paragraph + "\n\n"
            else:
                current_chunk += paragraph + "\n\n"

        if current_chunk:
            chunks.append({"type": "explanation", "content": current_chunk.strip()})

        return chunks

    def _chunk_mixed_content(self, section: dict) -> List[dict]:
        """Handle mixed content sections."""
        # For now, treat as regular text
        return self._chunk_text(section)

    def _chunk_text(self, section: dict) -> List[dict]:
        """Handle regular text chunking."""
        content = section["content"]
        if len(content) <= self.max_message_length:
            return [{"type": "text", "content": content}]

        # Split at natural break points
        chunks = []
        current_chunk = ""

        sentences = content.split(". ")
        for sentence in sentences:
            test_chunk = current_chunk + sentence + ". "
            if len(test_chunk) > self.max_message_length:
                if current_chunk:
                    chunks.append({"type": "text", "content": current_chunk.strip()})
                current_chunk = sentence + ". "
            else:
                current_chunk = test_chunk

        if current_chunk:
            chunks.append({"type": "text", "content": current_chunk.strip()})

        return chunks

    def _format_file_operations_section(self, section: dict) -> dict:
        """Format file operations section."""
        return {"type": "file_operations", "content": section["content"]}

    def _format_chunk(self, chunk: dict) -> List[FormattedMessage]:
        """Format individual chunks into FormattedMessage objects."""
        chunk_type = chunk["type"]
        content = chunk["content"]

        if chunk_type == "code_block":
            # Format code blocks with proper styling
            if chunk.get("format") == "split":
                title = (
                    ":page_facing_up: *Code (continued)*"
                    if "continued" in content
                    else ":page_facing_up: *Code*"
                )
            else:
                title = ":page_facing_up: *Code*"

            text = f"{title}\n\n{content}"

        elif chunk_type == "file_operations":
            text = f":file_folder: *File Operations*\n\n{content}"

        elif chunk_type == "explanation":
            text = content

        else:
            text = content

        # Split if still too long
        return self._split_message(text)

    def _get_contextual_keyboard(
        self, context: Optional[dict]
    ) -> Optional[List[dict]]:
        """Get context-aware quick action keyboard as Block Kit actions."""
        if not context:
            return self._get_quick_actions_keyboard()

        elements: List[dict] = []

        # Add context-specific buttons
        if context.get("has_code"):
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": ":floppy_disk: Save Code"},
                "action_id": "save_code",
                "value": "save_code",
            })

        if context.get("has_file_operations"):
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": ":file_folder: Show Files"},
                "action_id": "show_files",
                "value": "show_files",
            })

        if context.get("has_errors"):
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": ":wrench: Debug"},
                "action_id": "debug",
                "value": "debug",
            })

        # Add default actions
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": ":arrows_counterclockwise: Continue"},
            "action_id": "continue",
            "value": "continue",
        })
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": ":bulb: Explain"},
            "action_id": "explain",
            "value": "explain",
        })

        if not elements:
            return None

        return [{"type": "actions", "elements": elements}]

    def _clean_text(self, text: str) -> str:
        """Clean text for Slack display."""
        # Remove excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Convert markdown to Slack mrkdwn
        text = markdown_to_slack_mrkdwn(text)

        return text.strip()

    def _format_code_blocks(self, text: str) -> str:
        """Ensure code blocks are properly formatted for Slack.

        markdown_to_slack_mrkdwn already handles code blocks.
        This method now just truncates oversized code blocks.
        """

        def _truncate_code(m: re.Match) -> str:  # type: ignore[type-arg]
            full = m.group(0)
            if len(full) > self.max_code_block_length:
                # Re-extract and truncate the inner content
                inner = m.group(1)
                truncated = inner[: self.max_code_block_length - 80]
                return f"```\n{truncated}\n... (truncated)```"
            return full

        return re.sub(
            r"```(?:\w+)?\n(.*?)```",
            _truncate_code,
            text,
            flags=re.DOTALL,
        )

    def _split_message(self, text: str) -> List[FormattedMessage]:
        """Split long messages while preserving formatting."""
        if len(text) <= self.max_message_length:
            return [FormattedMessage(text)]

        messages = []
        current_lines: List[str] = []
        current_length = 0
        in_code_block = False

        lines = text.split("\n")

        for line in lines:
            line_length = len(line) + 1  # +1 for newline

            # Track ``` code block state
            stripped = line.strip()
            if stripped.startswith("```"):
                if not in_code_block:
                    in_code_block = True
                else:
                    in_code_block = False

            # If this is a very long line that exceeds limit by itself, split it
            if line_length > self.max_message_length:
                chunk_size = self.max_message_length - 100
                sub_chunks = []
                for i in range(0, len(line), chunk_size):
                    sub_chunks.append(line[i : i + chunk_size])

                for chunk in sub_chunks:
                    chunk_length = len(chunk) + 1

                    if (
                        current_length + chunk_length > self.max_message_length
                        and current_lines
                    ):
                        if in_code_block:
                            current_lines.append("```")
                        messages.append(FormattedMessage("\n".join(current_lines)))

                        current_lines = []
                        current_length = 0
                        if in_code_block:
                            current_lines.append("```")
                            current_length = 4

                    current_lines.append(chunk)
                    current_length += chunk_length
                continue

            # Check if adding this line would exceed the limit
            if current_length + line_length > self.max_message_length and current_lines:
                if in_code_block:
                    current_lines.append("```")

                messages.append(FormattedMessage("\n".join(current_lines)))

                current_lines = []
                current_length = 0

                if in_code_block:
                    current_lines.append("```")
                    current_length = 4

            current_lines.append(line)
            current_length += line_length

        # Add remaining content
        if current_lines:
            messages.append(FormattedMessage("\n".join(current_lines)))

        return messages

    def _get_quick_actions_keyboard(self) -> List[dict]:
        """Get quick actions as Block Kit actions blocks."""
        return [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":test_tube: Test"},
                        "action_id": "quick_test",
                        "value": "test",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":package: Install"},
                        "action_id": "quick_install",
                        "value": "install",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":art: Format"},
                        "action_id": "quick_format",
                        "value": "format",
                    },
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":mag: Find TODOs"},
                        "action_id": "quick_find_todos",
                        "value": "find_todos",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":hammer: Build"},
                        "action_id": "quick_build",
                        "value": "build",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":bar_chart: Git Status"},
                        "action_id": "quick_git_status",
                        "value": "git_status",
                    },
                ],
            },
        ]

    def create_confirmation_keyboard(
        self, confirm_data: str, cancel_data: str = "confirm:no"
    ) -> List[dict]:
        """Create a confirmation keyboard as Block Kit actions."""
        return [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":white_check_mark: Yes"},
                        "action_id": f"confirm_{confirm_data}",
                        "value": confirm_data,
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":x: No"},
                        "action_id": f"cancel_{cancel_data}",
                        "value": cancel_data,
                        "style": "danger",
                    },
                ],
            }
        ]

    def create_navigation_keyboard(self, options: List[tuple]) -> List[dict]:
        """Create navigation keyboard from options list as Block Kit actions.

        Args:
            options: List of (text, callback_data) tuples
        """
        elements: List[dict] = []
        actions_blocks: List[dict] = []

        for text, callback_data in options:
            # Sanitize callback_data into a valid action_id (alphanumeric + underscores)
            action_id = re.sub(r"[^a-zA-Z0-9_]", "_", callback_data)
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": text},
                "action_id": action_id,
                "value": callback_data,
            })

            # Slack allows up to 25 elements per actions block, but
            # for visual clarity we group in rows of 2 (matching the
            # original Telegram layout).
            if len(elements) == 2:
                actions_blocks.append({"type": "actions", "elements": elements})
                elements = []

        # Add remaining buttons
        if elements:
            actions_blocks.append({"type": "actions", "elements": elements})

        return actions_blocks


class ProgressIndicator:
    """Helper for creating progress indicators."""

    @staticmethod
    def create_bar(
        percentage: float,
        length: int = 10,
        filled_char: str = "\u2593",
        empty_char: str = "\u2591",
    ) -> str:
        """Create a progress bar."""
        filled = int((percentage / 100) * length)
        empty = length - filled
        return filled_char * filled + empty_char * empty

    @staticmethod
    def create_spinner(step: int) -> str:
        """Create a spinning indicator."""
        spinners = ["\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834", "\u2826", "\u2827", "\u2807", "\u280f"]
        return spinners[step % len(spinners)]

    @staticmethod
    def create_dots(step: int) -> str:
        """Create a dots indicator."""
        dots = ["", ".", "..", "..."]
        return dots[step % len(dots)]


class CodeHighlighter:
    """Simple code highlighting for common languages."""

    # Language file extensions mapping
    LANGUAGE_EXTENSIONS = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "javascript",
        ".tsx": "typescript",
        ".java": "java",
        ".cpp": "cpp",
        ".c": "c",
        ".cs": "csharp",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".php": "php",
        ".swift": "swift",
        ".kt": "kotlin",
        ".scala": "scala",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".sql": "sql",
        ".json": "json",
        ".xml": "xml",
        ".html": "html",
        ".css": "css",
        ".scss": "scss",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".md": "markdown",
    }

    @classmethod
    def detect_language(cls, filename: str) -> str:
        """Detect programming language from filename."""
        from pathlib import Path

        ext = Path(filename).suffix.lower()
        return cls.LANGUAGE_EXTENSIONS.get(ext, "")

    @classmethod
    def format_code(cls, code: str, language: str = "", filename: str = "") -> str:
        """Format code with language detection, using Slack code blocks."""
        if not language and filename:
            language = cls.detect_language(filename)

        if language:
            return f"```{language}\n{code}```"
        else:
            return f"```\n{code}```"

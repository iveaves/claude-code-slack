"""
Handle image uploads for UI/screenshot analysis

Features:
- Image type detection
- Prompt generation for Claude
- Support for Slack file_shared events
"""

import base64
from dataclasses import dataclass
from typing import Dict, Optional

import aiohttp

from src.config import Settings


@dataclass
class ProcessedImage:
    """Processed image result"""

    prompt: str
    image_type: str
    base64_data: str
    size: int
    metadata: Dict[str, any] = None


class ImageHandler:
    """Process image uploads from Slack"""

    def __init__(self, config: Settings):
        self.config = config
        self.supported_formats = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

    async def process_image_from_slack(
        self,
        file_info: dict,
        bot_token: str,
        caption: Optional[str] = None,
    ) -> ProcessedImage:
        """Process an image uploaded to Slack.

        Args:
            file_info: Slack file info dict from files.info API
            bot_token: Bot token for downloading the file
            caption: Optional user caption/message
        """
        url = file_info.get("url_private_download") or file_info.get("url_private")
        if not url:
            raise ValueError("No download URL in file info")

        # Download the image using the bot token for auth
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {bot_token}"}
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise ValueError(f"Failed to download image: HTTP {resp.status}")
                image_bytes = await resp.read()

        image_type = self._detect_image_type(image_bytes)

        if image_type == "screenshot":
            prompt = self._create_screenshot_prompt(caption)
        elif image_type == "diagram":
            prompt = self._create_diagram_prompt(caption)
        elif image_type == "ui_mockup":
            prompt = self._create_ui_prompt(caption)
        else:
            prompt = self._create_generic_prompt(caption)

        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        return ProcessedImage(
            prompt=prompt,
            image_type=image_type,
            base64_data=base64_image,
            size=len(image_bytes),
            metadata={
                "format": self._detect_format(image_bytes),
                "has_caption": caption is not None,
                "filename": file_info.get("name", "unknown"),
            },
        )

    # Keep old method signature for compatibility
    async def process_image(
        self, photo_or_file_info: any, caption: Optional[str] = None
    ) -> ProcessedImage:
        """Process image — accepts either a Slack file_info dict or raw bytes."""
        if isinstance(photo_or_file_info, dict):
            # This path requires a bot token, which should be passed separately
            raise ValueError("Use process_image_from_slack() for Slack file_info dicts")
        # Fallback for raw bytes
        image_bytes = photo_or_file_info
        if isinstance(image_bytes, (bytearray, memoryview)):
            image_bytes = bytes(image_bytes)

        image_type = self._detect_image_type(image_bytes)
        prompt = self._create_generic_prompt(caption)
        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        return ProcessedImage(
            prompt=prompt,
            image_type=image_type,
            base64_data=base64_image,
            size=len(image_bytes),
            metadata={
                "format": self._detect_format(image_bytes),
                "has_caption": caption is not None,
            },
        )

    def _detect_image_type(self, image_bytes: bytes) -> str:
        """Detect type of image"""
        return "screenshot"

    def _detect_format(self, image_bytes: bytes) -> str:
        """Detect image format from magic bytes"""
        if image_bytes.startswith(b"\x89PNG"):
            return "png"
        elif image_bytes.startswith(b"\xff\xd8\xff"):
            return "jpeg"
        elif image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
            return "gif"
        elif image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:12]:
            return "webp"
        else:
            return "unknown"

    def _create_screenshot_prompt(self, caption: Optional[str]) -> str:
        """Create prompt for screenshot analysis"""
        base_prompt = """I'm sharing a screenshot with you. Please analyze it and help me with:

1. Identifying what application or website this is from
2. Understanding the UI elements and their purpose
3. Any issues or improvements you notice
4. Answering any specific questions I have

"""
        if caption:
            base_prompt += f"Specific request: {caption}"
        return base_prompt

    def _create_diagram_prompt(self, caption: Optional[str]) -> str:
        """Create prompt for diagram analysis"""
        base_prompt = """I'm sharing a diagram with you. Please help me:

1. Understand the components and their relationships
2. Identify the type of diagram (flowchart, architecture, etc.)
3. Explain any technical concepts shown
4. Suggest improvements or clarifications

"""
        if caption:
            base_prompt += f"Specific request: {caption}"
        return base_prompt

    def _create_ui_prompt(self, caption: Optional[str]) -> str:
        """Create prompt for UI mockup analysis"""
        base_prompt = """I'm sharing a UI mockup with you. Please analyze:

1. The layout and visual hierarchy
2. User experience considerations
3. Accessibility aspects
4. Implementation suggestions
5. Any potential improvements

"""
        if caption:
            base_prompt += f"Specific request: {caption}"
        return base_prompt

    def _create_generic_prompt(self, caption: Optional[str]) -> str:
        """Create generic image analysis prompt"""
        base_prompt = """I'm sharing an image with you. Please analyze it and provide relevant insights.

"""
        if caption:
            base_prompt += f"Context: {caption}"
        return base_prompt

    def supports_format(self, filename: str) -> bool:
        """Check if image format is supported"""
        if not filename:
            return False
        parts = filename.lower().split(".")
        if len(parts) < 2:
            return False
        extension = f".{parts[-1]}"
        return extension in self.supported_formats

    async def validate_image(self, image_bytes: bytes) -> tuple[bool, Optional[str]]:
        """Validate image data"""
        max_size = 10 * 1024 * 1024  # 10MB
        if len(image_bytes) > max_size:
            return False, "Image too large (max 10MB)"
        format_type = self._detect_format(image_bytes)
        if format_type == "unknown":
            return False, "Unsupported image format"
        if len(image_bytes) < 100:
            return False, "Invalid image data"
        return True, None

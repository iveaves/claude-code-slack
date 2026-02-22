"""Slack channel synchronization and project resolution."""

from dataclasses import dataclass
from typing import Optional

import structlog
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from ..storage.repositories import ProjectThreadRepository
from .registry import ProjectDefinition, ProjectRegistry

logger = structlog.get_logger()


class PrivateTopicsUnavailableError(RuntimeError):
    """Kept for backwards compatibility."""


@dataclass
class ChannelSyncResult:
    """Summary of a channel synchronization run."""

    created: int = 0
    reused: int = 0
    failed: int = 0
    deactivated: int = 0


TopicSyncResult = ChannelSyncResult


class ProjectChannelManager:
    """Maintains mapping between projects and Slack channels.

    Channels are auto-created with the naming convention #pan-{slug}.
    If a project already has a channel_id in the YAML config, that's used directly.
    Otherwise, the manager creates/finds channels automatically.
    """

    def __init__(
        self,
        registry: ProjectRegistry,
        repository: ProjectThreadRepository,
    ) -> None:
        self.registry = registry
        self.repository = repository
        # Runtime channel_id -> project mapping (built during sync)
        self._channel_map: dict[str, ProjectDefinition] = {}

    async def sync_channels(
        self, client: AsyncWebClient
    ) -> ChannelSyncResult:
        """Create/reconcile Slack channels for all enabled projects."""
        result = ChannelSyncResult()
        enabled = self.registry.list_enabled()

        # Get existing channels in workspace
        existing_channels = await self._list_channels(client)
        channel_map = {ch["name"]: ch for ch in existing_channels}
        channel_id_map = {ch["id"]: ch for ch in existing_channels}

        for project in enabled:
            try:
                # If project has a channel_id in YAML, use it directly
                if project.channel_id:
                    if project.channel_id in channel_id_map:
                        self._channel_map[project.channel_id] = project
                        result.reused += 1
                        logger.info(
                            "Project mapped to existing channel (from config)",
                            slug=project.slug,
                            channel_id=project.channel_id,
                        )
                    elif project.channel_id.startswith("D"):
                        # DM channels won't appear in conversations_list
                        # but are valid for project routing
                        self._channel_map[project.channel_id] = project
                        result.reused += 1
                        logger.info(
                            "Project mapped to DM channel (from config)",
                            slug=project.slug,
                            channel_id=project.channel_id,
                        )
                    else:
                        result.failed += 1
                        logger.warning(
                            "Configured channel_id not found in workspace",
                            slug=project.slug,
                            channel_id=project.channel_id,
                        )
                    continue

                # Auto-create/find channel with pan- prefix
                channel_name = self._project_channel_name(project.slug)

                # Check DB for existing mapping
                existing_mapping = await self.repository.get_by_project_slug(
                    project.slug,
                )
                if existing_mapping and existing_mapping.is_active:
                    self._channel_map[existing_mapping.channel_id] = project
                    result.reused += 1
                    continue

                # Check if channel already exists in Slack
                if channel_name in channel_map:
                    channel_id = channel_map[channel_name]["id"]
                    await self.repository.upsert_mapping(
                        project_slug=project.slug,
                        chat_id=0,
                        channel_id=channel_id,
                        topic_name=project.name,
                        is_active=True,
                    )
                    self._channel_map[channel_id] = project
                    result.reused += 1
                    continue

                # Create new channel
                channel_id = await self._create_channel(
                    client, channel_name, project.name
                )
                if channel_id:
                    await self.repository.upsert_mapping(
                        project_slug=project.slug,
                        chat_id=0,
                        channel_id=channel_id,
                        topic_name=project.name,
                        is_active=True,
                    )
                    self._channel_map[channel_id] = project
                    result.created += 1
                else:
                    result.failed += 1

            except SlackApiError as e:
                result.failed += 1
                logger.error(
                    "Failed to sync project channel",
                    project_slug=project.slug,
                    error=str(e),
                )
            except Exception as e:
                result.failed += 1
                logger.error(
                    "Failed to sync project channel",
                    project_slug=project.slug,
                    error=str(e),
                )

        return result

    async def sync_topics(self, client: AsyncWebClient, **kwargs) -> ChannelSyncResult:
        """Alias for sync_channels."""
        return await self.sync_channels(client)

    async def resolve_project(
        self, channel_id: str
    ) -> Optional[ProjectDefinition]:
        """Resolve mapped project for a Slack channel.

        Checks in order:
        1. Runtime channel map (populated during sync)
        2. Registry channel_id (from YAML config)
        3. Database mapping (from auto-created channels)
        """
        if not channel_id:
            return None

        # Check runtime map first (fastest)
        if channel_id in self._channel_map:
            project = self._channel_map[channel_id]
            if project.enabled:
                return project
            return None

        # Check registry (YAML channel_id)
        project = self.registry.get_by_channel_id(channel_id)
        if project and project.enabled:
            self._channel_map[channel_id] = project
            return project

        # Check database mapping
        mapping = await self.repository.get_by_channel_id(channel_id)
        if mapping:
            project = self.registry.get_by_slug(mapping.project_slug)
            if project and project.enabled:
                self._channel_map[channel_id] = project
                return project

        return None

    @staticmethod
    def guidance_message(**kwargs) -> str:
        """Guidance text for channel routing rejections."""
        return (
            "*Project Channel Required*\n\n"
            "This bot is configured for project channels.\n"
            "Please send messages in a `#pan-` project channel.\n\n"
            "Use `/sync_channels` to create/refresh project channels."
        )

    @staticmethod
    def _project_channel_name(slug: str) -> str:
        """Convert project slug to a Slack channel name."""
        name = f"pan-{slug}".lower()
        name = name.replace(" ", "-").replace("_", "-")
        return name[:80]

    async def _list_channels(self, client: AsyncWebClient) -> list:
        """List all channels in the workspace."""
        channels = []
        cursor = None
        while True:
            kwargs = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            response = await client.conversations_list(**kwargs)
            channels.extend(response.get("channels", []))
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return channels

    async def _create_channel(
        self, client: AsyncWebClient, name: str, purpose: str
    ) -> Optional[str]:
        """Create a Slack channel and return its ID."""
        try:
            response = await client.conversations_create(
                name=name,
                is_private=False,
            )
            channel_id = response["channel"]["id"]

            # Set purpose
            try:
                await client.conversations_setPurpose(
                    channel=channel_id,
                    purpose=f"Claude Code project: {purpose}",
                )
            except SlackApiError:
                pass

            # Post intro message
            try:
                await client.chat_postMessage(
                    channel=channel_id,
                    text=f"*{purpose}*\n\nThis channel is mapped to a project directory. Send messages here to work on this project with Claude.",
                )
            except SlackApiError:
                pass

            logger.info("Created project channel", name=name, channel_id=channel_id)
            return channel_id

        except SlackApiError as e:
            if e.response.get("error") == "name_taken":
                logger.warning("Channel name taken", name=name)
            else:
                logger.error("Failed to create channel", name=name, error=str(e))
            return None


# Keep old name as alias
ProjectThreadManager = ProjectChannelManager

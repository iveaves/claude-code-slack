"""Main Slack bot class.

Features:
- Slack Bolt App with Socket Mode
- Command registration
- Handler management
- Middleware chain
- Graceful shutdown
"""

from typing import Any, Dict, Optional

import structlog
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp

from ..config.settings import Settings
from ..exceptions import ClaudeCodeSlackError
from .features.registry import FeatureRegistry
from .orchestrator import MessageOrchestrator

logger = structlog.get_logger()


class ClaudeCodeBot:
    """Main bot orchestrator using Slack Bolt."""

    def __init__(self, settings: Settings, dependencies: Dict[str, Any]):
        """Initialize bot with settings and dependencies."""
        self.settings = settings
        self.deps = dependencies
        self.app: Optional[AsyncApp] = None
        self.socket_handler: Optional[AsyncSocketModeHandler] = None
        self.is_running = False
        self.feature_registry: Optional[FeatureRegistry] = None
        self.orchestrator = MessageOrchestrator(settings, dependencies)

    async def initialize(self) -> None:
        """Initialize bot application. Idempotent — safe to call multiple times."""
        if self.app is not None:
            return

        logger.info("Initializing Slack bot")

        # Create Slack Bolt async app
        self.app = AsyncApp(token=self.settings.slack_bot_token_str)

        # Initialize feature registry
        self.feature_registry = FeatureRegistry(
            config=self.settings,
            storage=self.deps.get("storage"),
            security=self.deps.get("security"),
        )

        # Add feature registry to dependencies
        self.deps["features"] = self.feature_registry

        # Add middleware
        self._add_middleware()

        # Register handlers
        self._register_handlers()

        logger.info("Bot initialization complete")

    def _register_handlers(self) -> None:
        """Register handlers via orchestrator (mode-aware)."""
        self.orchestrator.register_handlers(self.app)

    def _add_middleware(self) -> None:
        """Add middleware to Slack Bolt app.

        Bolt middleware runs in registration order.
        Each middleware calls `await next()` to continue the chain.
        """
        from .middleware.auth import slack_auth_middleware
        from .middleware.rate_limit import slack_rate_limit_middleware
        from .middleware.security import slack_security_middleware

        deps = self.deps
        settings = self.settings

        # Security middleware first (validate inputs)
        @self.app.middleware
        async def security_mw(body, event, context, next):
            context["deps"] = deps
            context["settings"] = settings
            await slack_security_middleware(body, event, context, next)

        # Authentication second
        @self.app.middleware
        async def auth_mw(body, event, context, next):
            context["deps"] = deps
            context["settings"] = settings
            await slack_auth_middleware(body, event, context, next)

        # Rate limiting third
        @self.app.middleware
        async def rate_limit_mw(body, event, context, next):
            context["deps"] = deps
            context["settings"] = settings
            await slack_rate_limit_middleware(body, event, context, next)

        logger.info("Middleware added to bot")

    async def start(self) -> None:
        """Start the bot with Socket Mode."""
        if self.is_running:
            logger.warning("Bot is already running")
            return

        await self.initialize()

        logger.info("Starting bot", mode="socket_mode")

        try:
            self.is_running = True

            self.socket_handler = AsyncSocketModeHandler(
                self.app, self.settings.slack_app_token_str
            )

            # start() is blocking — it runs until stopped
            await self.socket_handler.start_async()

        except Exception as e:
            logger.error("Error running bot", error=str(e))
            raise ClaudeCodeSlackError(f"Failed to start bot: {str(e)}") from e
        finally:
            self.is_running = False

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if not self.is_running:
            logger.warning("Bot is not running")
            return

        logger.info("Stopping bot")

        try:
            self.is_running = False

            # Shutdown feature registry
            if self.feature_registry:
                self.feature_registry.shutdown()

            if self.socket_handler:
                await self.socket_handler.close_async()

            logger.info("Bot stopped successfully")
        except Exception as e:
            logger.error("Error stopping bot", error=str(e))
            raise ClaudeCodeSlackError(f"Failed to stop bot: {str(e)}") from e

    async def get_bot_info(self) -> Dict[str, Any]:
        """Get bot information."""
        if not self.app:
            return {"status": "not_initialized"}

        try:
            from slack_sdk.web.async_client import AsyncWebClient

            client: AsyncWebClient = self.app.client
            response = await client.auth_test()
            return {
                "status": "running" if self.is_running else "initialized",
                "bot_id": response.get("bot_id"),
                "user_id": response.get("user_id"),
                "team": response.get("team"),
                "team_id": response.get("team_id"),
                "url": response.get("url"),
            }
        except Exception as e:
            logger.error("Failed to get bot info", error=str(e))
            return {"status": "error", "error": str(e)}

    async def health_check(self) -> bool:
        """Perform health check."""
        try:
            if not self.app:
                return False
            await self.app.client.auth_test()
            return True
        except Exception as e:
            logger.error("Health check failed", error=str(e))
            return False

"""Main entry point for Claude Code Slack Bot."""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import structlog
from slack_sdk.web.async_client import AsyncWebClient

from src import __version__
from src.bot.core import ClaudeCodeBot
from src.claude import (
    ClaudeIntegration,
    SessionManager,
    ToolMonitor,
)
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.features import FeatureFlags
from src.config.settings import Settings
from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.middleware import EventSecurityMiddleware
from src.exceptions import ConfigurationError
from src.notifications.service import NotificationService
from src.projects import ProjectChannelManager, load_project_registry
from src.scheduler.scheduler import JobScheduler
from src.security.audit import AuditLogger, InMemoryAuditStorage
from src.security.auth import (
    AuthenticationManager,
    InMemoryTokenStorage,
    TokenAuthProvider,
    WhitelistAuthProvider,
)
from src.security.rate_limiter import RateLimiter
from src.security.validators import SecurityValidator
from src.storage.facade import Storage
from src.storage.session_storage import SQLiteSessionStorage


def setup_logging(debug: bool = False) -> None:
    """Configure structured logging."""
    level = logging.DEBUG if debug else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            (
                structlog.processors.JSONRenderer()
                if not debug
                else structlog.dev.ConsoleRenderer()
            ),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Claude Code Slack Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--version", action="version", version=f"Claude Code Slack Bot {__version__}"
    )

    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    parser.add_argument("--config-file", type=Path, help="Path to configuration file")

    return parser.parse_args()


async def create_application(config: Settings) -> Dict[str, Any]:
    """Create and configure the application components."""
    logger = structlog.get_logger()
    logger.info("Creating application components")

    features = FeatureFlags(config)

    # Initialize storage system
    storage = Storage(config.database_url)
    await storage.initialize()

    # Create security components
    providers = []

    if config.allowed_users:
        providers.append(WhitelistAuthProvider(config.allowed_users))

    if config.enable_token_auth:
        token_storage = InMemoryTokenStorage()
        providers.append(TokenAuthProvider(config.auth_token_secret, token_storage))

    if not providers and config.development_mode:
        logger.warning(
            "No auth providers configured"
            " - creating development-only allow-all provider"
        )
        providers.append(WhitelistAuthProvider([], allow_all_dev=True))
    elif not providers:
        raise ConfigurationError("No authentication providers configured")

    auth_manager = AuthenticationManager(providers)
    security_validator = SecurityValidator(
        config.approved_directory,
        disable_security_patterns=config.disable_security_patterns,
        development_mode=config.development_mode,
    )
    rate_limiter = RateLimiter(config)

    audit_storage = InMemoryAuditStorage()
    audit_logger = AuditLogger(audit_storage)

    # Create Claude integration components
    session_storage = SQLiteSessionStorage(storage.db_manager)
    session_manager = SessionManager(config, session_storage)
    tool_monitor = ToolMonitor(
        config, security_validator, agentic_mode=config.agentic_mode
    )

    # Create Claude backend based on configuration
    if getattr(config, "use_sdk", True):
        logger.info("Using Claude Python SDK integration")
        sdk_manager = ClaudeSDKManager(config)
        claude_integration = ClaudeIntegration(
            config=config,
            sdk_manager=sdk_manager,
            session_manager=session_manager,
            tool_monitor=tool_monitor,
        )
    else:
        from src.claude.cli_integration import ClaudeProcessManager

        logger.info("Using Claude CLI subprocess integration")
        process_manager = ClaudeProcessManager(config)
        claude_integration = ClaudeIntegration(
            config=config,
            process_manager=process_manager,
            session_manager=session_manager,
            tool_monitor=tool_monitor,
        )

    # Event bus and agentic platform components
    event_bus = EventBus()

    event_security = EventSecurityMiddleware(
        event_bus=event_bus,
        security_validator=security_validator,
        auth_manager=auth_manager,
    )
    event_security.register()

    # Create bot with all dependencies
    dependencies = {
        "auth_manager": auth_manager,
        "security_validator": security_validator,
        "rate_limiter": rate_limiter,
        "audit_logger": audit_logger,
        "claude_integration": claude_integration,
        "storage": storage,
        "event_bus": event_bus,
        "project_registry": None,
        "project_channels_manager": None,
    }

    bot = ClaudeCodeBot(config, dependencies)

    # Create agent handler with orchestrator reference for shared sessions
    from slack_sdk.web.async_client import AsyncWebClient

    slack_client = AsyncWebClient(token=config.slack_bot_token_str)
    agent_handler = AgentHandler(
        event_bus=event_bus,
        claude_integration=claude_integration,
        default_working_directory=config.approved_directory,
        default_user_id=config.allowed_users[0] if config.allowed_users else "",
        slack_client=slack_client,
        orchestrator=bot.orchestrator,
    )
    agent_handler.register()

    logger.info("Application components created successfully")

    return {
        "bot": bot,
        "claude_integration": claude_integration,
        "storage": storage,
        "config": config,
        "features": features,
        "event_bus": event_bus,
        "agent_handler": agent_handler,
        "auth_manager": auth_manager,
        "security_validator": security_validator,
    }


async def run_application(app: Dict[str, Any]) -> None:
    """Run the application with graceful shutdown handling."""
    logger = structlog.get_logger()
    bot: ClaudeCodeBot = app["bot"]
    claude_integration: ClaudeIntegration = app["claude_integration"]
    storage: Storage = app["storage"]
    config: Settings = app["config"]
    features: FeatureFlags = app["features"]
    event_bus: EventBus = app["event_bus"]

    notification_service: Optional[NotificationService] = None
    scheduler: Optional[JobScheduler] = None

    shutdown_event = asyncio.Event()

    def signal_handler(signum: int, frame: Any) -> None:
        logger.info("Shutdown signal received", signal=signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("Starting Claude Code Slack Bot")

        # Initialize the bot first (creates the Slack Bolt App)
        await bot.initialize()

        # Create Slack WebClient (shared for notifications + channel management)
        slack_client = AsyncWebClient(token=config.slack_bot_token_str)

        if config.enable_project_channels:
            if not config.projects_config_path:
                raise ConfigurationError(
                    "Project channel mode enabled but required settings are missing"
                )
            registry = load_project_registry(
                config_path=config.projects_config_path,
                approved_directory=config.approved_directory,
            )

            from src.projects import ProjectChannelManager

            channel_manager = ProjectChannelManager(
                registry=registry,
                repository=storage.project_threads,
            )

            bot.deps["project_registry"] = registry
            bot.deps["project_channels_manager"] = channel_manager

            # Sync channels on startup (creates missing ones)
            sync_result = await channel_manager.sync_channels(slack_client)
            logger.info(
                "Project channel sync complete",
                created=sync_result.created,
                reused=sync_result.reused,
                failed=sync_result.failed,
            )

        # Start event bus
        await event_bus.start()

        # Notification service
        notification_service = NotificationService(
            event_bus=event_bus,
            client=slack_client,
            default_channel_ids=config.notification_channel_ids or [],
        )
        notification_service.register()
        await notification_service.start()

        # Collect concurrent tasks
        tasks = []

        # Bot task
        bot_task = asyncio.create_task(bot.start())
        tasks.append(bot_task)

        # API server (if enabled)
        if features.api_server_enabled:
            from src.api.server import run_api_server

            api_task = asyncio.create_task(
                run_api_server(event_bus, config, storage.db_manager)
            )
            tasks.append(api_task)
            logger.info("API server enabled", port=config.api_server_port)

        # Scheduler (if enabled)
        if features.scheduler_enabled:
            scheduler = JobScheduler(
                event_bus=event_bus,
                db_manager=storage.db_manager,
                default_working_directory=config.approved_directory,
                timezone=config.scheduler_timezone,
            )
            await scheduler.start()
            bot.deps["scheduler"] = scheduler
            logger.info("Job scheduler enabled")

        # Shutdown task
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        tasks.append(shutdown_task)

        # Wait for any task to complete or shutdown signal
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "Task failed",
                    task=task.get_name(),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error("Application error", error=str(e))
        raise
    finally:
        logger.info("Shutting down application")

        try:
            if scheduler:
                await scheduler.stop()
            if notification_service:
                await notification_service.stop()
            await event_bus.stop()
            await bot.stop()
            await claude_integration.shutdown()
            await storage.close()
        except Exception as e:
            logger.error("Error during shutdown", error=str(e))

        logger.info("Application shutdown complete")


PIDFILE = Path("data/bot.pid")


def _acquire_pidfile() -> None:
    """Kill any existing bot instance and write our PID.

    Prevents duplicate Socket Mode connections which cause double responses.
    """
    if PIDFILE.exists():
        try:
            old_pid = int(PIDFILE.read_text().strip())
            # Check if old process is actually a bot (not a recycled PID)
            try:
                os.kill(old_pid, 0)  # probe — doesn't actually kill
                # Process exists, kill it
                os.kill(old_pid, signal.SIGTERM)
                import time

                time.sleep(2)  # give it time to shut down
            except ProcessNotFoundError:
                pass  # already dead
            except PermissionError:
                pass  # different user's process, skip
        except (ValueError, OSError):
            pass  # corrupt PID file, ignore

    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))


def _release_pidfile() -> None:
    """Remove PID file on shutdown."""
    try:
        PIDFILE.unlink(missing_ok=True)
    except OSError:
        pass


async def main() -> None:
    """Main application entry point."""
    args = parse_args()
    setup_logging(debug=args.debug)

    _acquire_pidfile()

    logger = structlog.get_logger()
    logger.info("Starting Claude Code Slack Bot", version=__version__)

    try:
        from src.config import FeatureFlags, load_config

        config = load_config(config_file=args.config_file)
        features = FeatureFlags(config)

        logger.info(
            "Configuration loaded",
            environment="production" if config.is_production else "development",
            enabled_features=features.get_enabled_features(),
            debug=config.debug,
        )

        app = await create_application(config)
        await run_application(app)

    except ConfigurationError as e:
        logger.error("Configuration error", error=str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception("Unexpected error", error=str(e))
        sys.exit(1)


def run() -> None:
    """Synchronous entry point for setuptools."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested by user")
    finally:
        _release_pidfile()
    sys.exit(0)


if __name__ == "__main__":
    run()

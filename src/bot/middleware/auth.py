"""Slack bot authentication middleware."""

from datetime import UTC, datetime
from typing import Any, Callable

import structlog

logger = structlog.get_logger()


def _extract_user_id(body: dict, event: Any) -> str | None:
    """Extract Slack user ID from body or event."""
    # Slash commands put user_id in body
    user_id = body.get("user_id")
    if user_id:
        return str(user_id)
    # Action/interaction payloads put user in body["user"]["id"]
    if isinstance(body.get("user"), dict):
        user_id = body["user"].get("id")
        if user_id:
            return str(user_id)
    # Message events put user in event dict
    if isinstance(event, dict):
        return event.get("user")
    return None


async def slack_auth_middleware(
    body: dict, event: Any, context: dict, next: Callable
) -> None:
    """Check authentication before processing messages.

    This middleware:
    1. Checks if user is authenticated
    2. Attempts authentication if not authenticated
    3. Updates session activity
    4. Logs authentication events
    """
    user_id = _extract_user_id(body, event)

    if not user_id:
        logger.warning("No user information in event")
        return

    deps = context.get("deps", {})
    auth_manager = deps.get("auth_manager")
    audit_logger = deps.get("audit_logger")

    if not auth_manager:
        logger.error("Authentication manager not available in middleware context")
        return

    # Check if user is already authenticated
    if auth_manager.is_authenticated(user_id):
        if auth_manager.refresh_session(user_id):
            session = auth_manager.get_session(user_id)
            logger.debug(
                "Session refreshed",
                user_id=user_id,
                auth_provider=session.auth_provider if session else None,
            )
        # Store user_id in context for downstream handlers
        context["user_id"] = user_id
        await next()
        return

    # User not authenticated - attempt authentication
    logger.info("Attempting authentication for user", user_id=user_id)

    authentication_successful = await auth_manager.authenticate_user(user_id)

    if audit_logger:
        await audit_logger.log_auth_attempt(
            user_id=user_id,
            success=authentication_successful,
            method="automatic",
            reason="message_received",
        )

    if authentication_successful:
        session = auth_manager.get_session(user_id)
        logger.info(
            "User authenticated successfully",
            user_id=user_id,
            auth_provider=session.auth_provider if session else None,
        )
        context["user_id"] = user_id
        await next()
        return
    else:
        logger.warning("Authentication failed", user_id=user_id)
        # Don't call next() — stops the middleware chain
        return


async def require_auth(body: dict, event: Any, context: dict, next: Callable) -> None:
    """Stricter middleware that only allows authenticated users."""
    user_id = _extract_user_id(body, event)
    deps = context.get("deps", {})
    auth_manager = deps.get("auth_manager")

    if not auth_manager or not auth_manager.is_authenticated(user_id):
        return

    context["user_id"] = user_id
    await next()


async def admin_required(body: dict, event: Any, context: dict, next: Callable) -> None:
    """Middleware that requires admin privileges."""
    user_id = _extract_user_id(body, event)
    deps = context.get("deps", {})
    auth_manager = deps.get("auth_manager")

    if not auth_manager or not auth_manager.is_authenticated(user_id):
        return

    session = auth_manager.get_session(user_id)
    if not session or not session.user_info:
        return

    permissions = session.user_info.get("permissions", [])
    if "admin" not in permissions:
        return

    context["user_id"] = user_id
    await next()


# Keep old name for backwards compatibility
auth_middleware = slack_auth_middleware

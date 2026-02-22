"""Custom exceptions for Claude Code Slack Bot."""


class ClaudeCodeSlackError(Exception):
    """Base exception for Claude Code Slack Bot."""


# Keep old name as alias for any remaining references during migration
ClaudeCodeTelegramError = ClaudeCodeSlackError


class ConfigurationError(ClaudeCodeSlackError):
    """Configuration-related errors."""


class MissingConfigError(ConfigurationError):
    """Required configuration is missing."""


class InvalidConfigError(ConfigurationError):
    """Configuration is invalid."""


class SecurityError(ClaudeCodeSlackError):
    """Security-related errors."""


class AuthenticationError(SecurityError):
    """Authentication failed."""


class AuthorizationError(SecurityError):
    """Authorization failed."""


class DirectoryTraversalError(SecurityError):
    """Directory traversal attempt detected."""


class ClaudeError(ClaudeCodeSlackError):
    """Claude Code-related errors."""


class ClaudeTimeoutError(ClaudeError):
    """Claude Code operation timed out."""


class ClaudeProcessError(ClaudeError):
    """Claude Code process execution failed."""


class ClaudeParsingError(ClaudeError):
    """Failed to parse Claude Code output."""


class StorageError(ClaudeCodeSlackError):
    """Storage-related errors."""


class DatabaseConnectionError(StorageError):
    """Database connection failed."""


class DataIntegrityError(StorageError):
    """Data integrity check failed."""


class SlackError(ClaudeCodeSlackError):
    """Slack API-related errors."""


# Keep old name as alias
TelegramError = SlackError


class MessageTooLongError(SlackError):
    """Message exceeds Slack's length limit."""


class RateLimitError(SlackError):
    """Rate limit exceeded."""


class RateLimitExceeded(RateLimitError):
    """Rate limit exceeded (alias for compatibility)."""

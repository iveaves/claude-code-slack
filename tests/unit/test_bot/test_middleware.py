"""Tests for Slack bot middleware (auth, rate limit, security).

Verifies that the middleware chain correctly gates message processing:
authentication rejects unknown users, rate limiting throttles excessive
requests, and security validation blocks dangerous input patterns.
"""

from src.config.settings import Settings
from src.security.auth import AuthenticationManager, WhitelistAuthProvider
from src.security.rate_limiter import RateLimiter
from src.security.validators import SecurityValidator


def _make_settings(tmp_path, **overrides):
    defaults = {
        "_env_file": None,
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
        "approved_directory": str(tmp_path),
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestWhitelistAuth:
    """Test whitelist-based authentication."""

    async def test_allowed_user_passes(self):
        provider = WhitelistAuthProvider(["U01GOOD", "U02ALSO"])
        result = await provider.authenticate("U01GOOD", {})
        assert result is True

    async def test_unknown_user_rejected(self):
        provider = WhitelistAuthProvider(["U01GOOD"])
        result = await provider.authenticate("U99BAD", {})
        assert result is False

    async def test_empty_whitelist_rejects_all(self):
        provider = WhitelistAuthProvider([])
        result = await provider.authenticate("U01ANY", {})
        assert result is False

    async def test_dev_mode_allows_all(self):
        provider = WhitelistAuthProvider([], allow_all_dev=True)
        result = await provider.authenticate("U99ANYONE", {})
        assert result is True


class TestAuthenticationManager:
    """Test multi-provider authentication."""

    async def test_whitelist_provider(self):
        whitelist = WhitelistAuthProvider(["U01GOOD"])
        manager = AuthenticationManager([whitelist])
        session = await manager.authenticate_user("U01GOOD")
        assert session is not None

    async def test_is_authenticated(self):
        whitelist = WhitelistAuthProvider(["U01GOOD"])
        manager = AuthenticationManager([whitelist])
        await manager.authenticate_user("U01GOOD")
        assert manager.is_authenticated("U01GOOD") is True
        assert manager.is_authenticated("U99BAD") is False


class TestSecurityValidator:
    """Test input validation and path security."""

    def test_validate_safe_path(self, tmp_path):
        validator = SecurityValidator(tmp_path)
        test_file = tmp_path / "safe.py"
        test_file.touch()
        valid, resolved, error = validator.validate_path("safe.py", tmp_path)
        assert valid is True

    def test_reject_path_traversal(self, tmp_path):
        validator = SecurityValidator(tmp_path)
        valid, resolved, error = validator.validate_path("../../etc/passwd", tmp_path)
        assert valid is False

    def test_reject_path_with_traversal_pattern(self, tmp_path):
        """Paths with shell metacharacters are blocked."""
        validator = SecurityValidator(tmp_path)
        valid, resolved, error = validator.validate_path("$(whoami)/file.txt", tmp_path)
        assert valid is False


class TestRateLimiter:
    """Test rate limiting."""

    async def test_allows_within_limit(self, tmp_path):
        config = _make_settings(tmp_path, rate_limit_requests=10, rate_limit_window=60)
        limiter = RateLimiter(config)
        allowed, msg = await limiter.check_rate_limit("U01USER", 0.001)
        assert allowed is True

    async def test_blocks_over_limit(self, tmp_path):
        config = _make_settings(
            tmp_path,
            rate_limit_requests=2,
            rate_limit_window=60,
            rate_limit_burst=2,
        )
        limiter = RateLimiter(config)
        for _ in range(3):
            await limiter.check_rate_limit("U01USER", 0.001)
        allowed, msg = await limiter.check_rate_limit("U01USER", 0.001)
        assert allowed is False

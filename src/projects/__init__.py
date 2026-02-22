"""Project registry and Slack channel management."""

from .registry import ProjectDefinition, ProjectRegistry, load_project_registry
from .thread_manager import (
    PrivateTopicsUnavailableError,
    ProjectChannelManager,
    ProjectThreadManager,
)

__all__ = [
    "ProjectDefinition",
    "ProjectRegistry",
    "load_project_registry",
    "ProjectChannelManager",
    "ProjectThreadManager",
    "PrivateTopicsUnavailableError",
]

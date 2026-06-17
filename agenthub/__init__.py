"""Agent Hub -- a Discord-like communication hub for agents on a shared filesystem."""

from .client import HubClient
from .store import HubStore

__version__ = "1.0.0"
__all__ = ["HubClient", "HubStore"]

"""Abstract base class for agent execution backends."""

from __future__ import annotations

from abc import ABC, abstractmethod

from goa.models import BackendResult


class AgentBackend(ABC):
    """Unified interface for agent execution backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier, e.g. 'cursor'."""

    @abstractmethod
    def execute(
        self,
        prompt: str,
        workspace: str,
        session_id: str | None = None,
    ) -> BackendResult:
        """Run a single agent invocation and return the result."""

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Check whether the CLI tool for this backend is installed."""

    @classmethod
    @abstractmethod
    def install_hint(cls) -> str:
        """Return a human-readable installation hint."""

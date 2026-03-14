"""Abstract base class for agent execution backends."""

from __future__ import annotations

import subprocess
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
        """Run a single headless agent invocation and return the result."""

    @abstractmethod
    def build_interactive_cmd(
        self,
        prompt: str,
        workspace: str,
        session_id: str | None = None,
    ) -> list[str]:
        """Return the command list for an interactive (foreground) session."""

    def run_interactive(
        self,
        prompt: str,
        workspace: str,
        session_id: str | None = None,
    ) -> int:
        """Launch an interactive agent session in the foreground.

        stdin/stdout/stderr are inherited — the user talks to the agent
        directly.  Returns the process exit code.
        """
        cmd = self.build_interactive_cmd(prompt, workspace, session_id)
        proc = subprocess.run(cmd, cwd=workspace)
        return proc.returncode

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Check whether the CLI tool for this backend is installed."""

    @classmethod
    @abstractmethod
    def install_hint(cls) -> str:
        """Return a human-readable installation hint."""

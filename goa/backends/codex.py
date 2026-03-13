"""OpenAI Codex CLI backend."""

from __future__ import annotations

import shutil
import subprocess
import time

from goa.backends.base import AgentBackend
from goa.models import BackendResult


class CodexBackend(AgentBackend):

    @property
    def name(self) -> str:
        return "codex"

    def execute(
        self,
        prompt: str,
        workspace: str,
        session_id: str | None = None,
    ) -> BackendResult:
        if session_id:
            prompt = (
                f"[Previous session context]\n{session_id}\n\n"
                f"[New instruction]\n{prompt}"
            )

        cmd = ["codex", "--full-auto", "--quiet", prompt]

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=workspace,
            )
        except FileNotFoundError:
            return BackendResult(
                success=False,
                output="",
                error="codex CLI not found. Install via: npm install -g @openai/codex",
                duration_sec=time.monotonic() - start,
            )
        except subprocess.TimeoutExpired:
            return BackendResult(
                success=False,
                output="",
                error="codex timed out after 600s",
                duration_sec=600.0,
            )

        duration = time.monotonic() - start
        output = proc.stdout.strip()

        if proc.returncode != 0:
            return BackendResult(
                success=False,
                output=output,
                error=proc.stderr.strip() or f"exit code {proc.returncode}",
                duration_sec=duration,
            )

        return BackendResult(
            success=True,
            output=output,
            session_id=output[:200] if output else None,
            duration_sec=duration,
        )

    def build_interactive_cmd(
        self,
        prompt: str,
        workspace: str,
        session_id: str | None = None,
    ) -> list[str]:
        return ["codex", prompt]

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("codex") is not None

    @classmethod
    def install_hint(cls) -> str:
        return "Install Codex CLI: npm install -g @openai/codex"

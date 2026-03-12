"""Cursor Agent backend — drives the `agent` CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from goa.backends.base import AgentBackend
from goa.models import BackendResult


class CursorBackend(AgentBackend):

    @property
    def name(self) -> str:
        return "cursor"

    def _get_api_key(self) -> str | None:
        """Resolve API key from standard env vars."""
        key = os.environ.get("CURSOR_API_KEY", "")
        if key:
            return key
        key = os.environ.get("cursor-api-key", "")
        return key or None

    def execute(
        self,
        prompt: str,
        workspace: str,
        session_id: str | None = None,
    ) -> BackendResult:
        cmd = [
            "agent",
            "-p", prompt,
            "--output-format", "stream-json",
            "--trust",
        ]

        api_key = self._get_api_key()
        if api_key:
            cmd.extend(["--api-key", api_key])

        if session_id:
            cmd.extend(["--resume", session_id])

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
                error="agent CLI not found. Install via: curl https://cursor.com/install -fsS | bash",
                duration_sec=time.monotonic() - start,
            )
        except subprocess.TimeoutExpired:
            return BackendResult(
                success=False,
                output="",
                error="agent timed out after 600s",
                duration_sec=600.0,
            )

        duration = time.monotonic() - start
        new_session_id = session_id
        text_parts: list[str] = []

        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                text_parts.append(line)
                continue

            if "session_id" in obj:
                new_session_id = obj["session_id"]

            msg_type = obj.get("type", "")

            if msg_type == "assistant":
                content = obj.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block["text"])
                elif isinstance(content, str) and content:
                    text_parts.append(content)

            elif msg_type == "result":
                result_text = obj.get("result", "")
                if result_text:
                    text_parts.append(result_text)

        output = "\n".join(text_parts).strip()

        if proc.returncode != 0 and not output:
            return BackendResult(
                success=False,
                output=output,
                session_id=new_session_id,
                error=proc.stderr.strip() or f"exit code {proc.returncode}",
                duration_sec=duration,
            )

        return BackendResult(
            success=proc.returncode == 0,
            output=output,
            session_id=new_session_id,
            error=proc.stderr.strip() if proc.returncode != 0 else "",
            duration_sec=duration,
        )

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("agent") is not None

    @classmethod
    def install_hint(cls) -> str:
        return "Install: curl https://cursor.com/install -fsS | bash"

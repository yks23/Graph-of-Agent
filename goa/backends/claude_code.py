"""Claude Code CLI backend."""

from __future__ import annotations

import json
import shutil
import subprocess
import time

from goa.backends.base import AgentBackend
from goa.models import BackendResult


class ClaudeCodeBackend(AgentBackend):

    @property
    def name(self) -> str:
        return "claude_code"

    def execute(
        self,
        prompt: str,
        workspace: str,
        session_id: str | None = None,
    ) -> BackendResult:
        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
        ]
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
                error="claude CLI not found. Install via: npm install -g @anthropic-ai/claude-code",
                duration_sec=time.monotonic() - start,
            )
        except subprocess.TimeoutExpired:
            return BackendResult(
                success=False,
                output="",
                error="claude timed out after 600s",
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
            if obj.get("type") == "assistant":
                content = obj.get("message", {}).get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block["text"])
                elif isinstance(content, str):
                    text_parts.append(content)
            elif obj.get("type") == "result":
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
        return shutil.which("claude") is not None

    @classmethod
    def install_hint(cls) -> str:
        return "Install Claude Code: npm install -g @anthropic-ai/claude-code"

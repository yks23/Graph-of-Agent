"""Backend registry and factory."""

from __future__ import annotations

from goa.backends.base import AgentBackend

_REGISTRY: dict[str, type[AgentBackend]] = {}


def register(cls: type[AgentBackend]) -> type[AgentBackend]:
    """Decorator — register a backend class by its name property."""
    instance = cls()
    _REGISTRY[instance.name] = cls
    return cls


def get_backend(name: str) -> AgentBackend:
    """Instantiate a backend by name."""
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(f"Unknown backend '{name}'. Available: {available}")
    return _REGISTRY[name]()


def list_backends() -> list[dict]:
    """Return metadata for all registered backends."""
    result = []
    for name, cls in sorted(_REGISTRY.items()):
        result.append({
            "name": name,
            "available": cls.is_available(),
            "hint": cls.install_hint(),
        })
    return result


# Auto-register built-in backends
from goa.backends.cursor import CursorBackend  # noqa: E402
from goa.backends.codex import CodexBackend  # noqa: E402
from goa.backends.claude_code import ClaudeCodeBackend  # noqa: E402

register(CursorBackend)
register(CodexBackend)
register(ClaudeCodeBackend)

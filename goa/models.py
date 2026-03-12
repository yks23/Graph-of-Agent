"""Pure data models for GoA — no I/O, no side effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Graph definition models
# ---------------------------------------------------------------------------

@dataclass
class Transition:
    """A directed edge: when a node finishes with a given condition, activate the target."""

    target: str
    condition: str = "done"


@dataclass
class GraphNode:
    """A single node in the graph = one agent invocation."""

    name: str
    skill: str
    backend: str
    transitions: list[Transition] = field(default_factory=list)
    description: str = ""


@dataclass
class Graph:
    """A complete agent collaboration graph."""

    name: str
    entry: str
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    description: str = ""

    def validate(self) -> list[str]:
        """Return a list of validation errors (empty means valid)."""
        errors: list[str] = []

        if not self.name:
            errors.append("Graph name is empty")

        if self.entry not in self.nodes:
            errors.append(f"Entry node '{self.entry}' is not defined in nodes")

        node_names = set(self.nodes.keys())
        for name, node in self.nodes.items():
            if node.name != name:
                errors.append(
                    f"Node key '{name}' does not match node.name '{node.name}'"
                )
            for t in node.transitions:
                if t.target not in node_names:
                    errors.append(
                        f"Node '{name}' has transition to undefined target '{t.target}'"
                    )

        referenced: set[str] = {self.entry}
        for node in self.nodes.values():
            for t in node.transitions:
                referenced.add(t.target)
        orphans = node_names - referenced
        for o in orphans:
            errors.append(f"Node '{o}' is unreachable (not entry and no incoming edge)")

        return errors


# ---------------------------------------------------------------------------
# Runtime state models
# ---------------------------------------------------------------------------

class NodeStatus(str, Enum):
    IDLE = "idle"
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class NodeState:
    """Runtime state of a single node within a run."""

    status: NodeStatus = NodeStatus.IDLE
    session_id: str | None = None
    activated_by: str | None = None
    output: str = ""
    error: str = ""
    updated_at: str = ""


@dataclass
class GraphRun:
    """Global state of a single graph execution."""

    graph_name: str
    run_id: str
    started_at: str
    status: str = "running"
    node_states: dict[str, NodeState] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Backend result
# ---------------------------------------------------------------------------

@dataclass
class BackendResult:
    """Return value of a single backend invocation."""

    success: bool
    output: str
    session_id: str | None = None
    error: str = ""
    duration_sec: float = 0.0

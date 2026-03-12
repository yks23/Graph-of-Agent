"""File-system backed persistence for GoA graphs and runs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from goa.models import (
    Graph,
    GraphNode,
    GraphRun,
    NodeState,
    NodeStatus,
    Transition,
)


class GoAStorage:
    """All file I/O for GoA — graph definitions, run state, and logs."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        if base_dir is None:
            base_dir = Path.cwd()
        self._root = Path(base_dir) / ".goa"

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------
    # Graph definition CRUD
    # ------------------------------------------------------------------

    def list_graphs(self) -> list[str]:
        if not self._root.exists():
            return []
        return sorted(
            d.name
            for d in self._root.iterdir()
            if d.is_dir() and (d / "graph.json").exists()
        )

    def save_graph(self, graph: Graph) -> None:
        graph_dir = self._root / graph.name
        graph_dir.mkdir(parents=True, exist_ok=True)
        data = _graph_to_dict(graph)
        (graph_dir / "graph.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def load_graph(self, name: str) -> Graph:
        path = self._root / name / "graph.json"
        if not path.exists():
            raise FileNotFoundError(f"Graph '{name}' not found at {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return _dict_to_graph(data)

    def delete_graph(self, name: str) -> None:
        import shutil

        graph_dir = self._root / name
        if graph_dir.exists():
            shutil.rmtree(graph_dir)

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def create_run(self, graph_name: str) -> GraphRun:
        run_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        run = GraphRun(graph_name=graph_name, run_id=run_id, started_at=now)
        self._run_dir(graph_name, run_id).mkdir(parents=True, exist_ok=True)
        self._states_dir(graph_name, run_id).mkdir(parents=True, exist_ok=True)
        self._logs_dir(graph_name, run_id).mkdir(parents=True, exist_ok=True)
        self.save_run(run)
        return run

    def load_run(self, graph_name: str, run_id: str) -> GraphRun:
        path = self._run_dir(graph_name, run_id) / "run.json"
        if not path.exists():
            raise FileNotFoundError(f"Run '{run_id}' not found for graph '{graph_name}'")
        data = json.loads(path.read_text(encoding="utf-8"))
        return _dict_to_run(data)

    def save_run(self, run: GraphRun) -> None:
        path = self._run_dir(run.graph_name, run.run_id) / "run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_run_to_dict(run), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def list_runs(self, graph_name: str) -> list[str]:
        runs_dir = self._root / graph_name / "runs"
        if not runs_dir.exists():
            return []
        return sorted(
            d.name for d in runs_dir.iterdir() if d.is_dir() and (d / "run.json").exists()
        )

    def latest_run_id(self, graph_name: str) -> str | None:
        runs = self.list_runs(graph_name)
        if not runs:
            return None
        latest = max(
            runs,
            key=lambda rid: self.load_run(graph_name, rid).started_at,
        )
        return latest

    # ------------------------------------------------------------------
    # Node state (atomic read / write — the "message bus")
    # ------------------------------------------------------------------

    def read_node_state(self, graph_name: str, run_id: str, node: str) -> NodeState:
        path = self._states_dir(graph_name, run_id) / f"{node}.json"
        if not path.exists():
            return NodeState()
        data = json.loads(path.read_text(encoding="utf-8"))
        return _dict_to_node_state(data)

    def write_node_state(
        self, graph_name: str, run_id: str, node: str, state: NodeState
    ) -> None:
        state.updated_at = datetime.now(timezone.utc).isoformat()
        path = self._states_dir(graph_name, run_id) / f"{node}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_node_state_to_dict(state), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    def append_log(self, graph_name: str, run_id: str, node: str, line: str) -> None:
        path = self._logs_dir(graph_name, run_id) / f"{node}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            f.write(f"[{ts}] {line}\n")

    def read_log(self, graph_name: str, run_id: str, node: str) -> str:
        path = self._logs_dir(graph_name, run_id) / f"{node}.log"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Internal path helpers
    # ------------------------------------------------------------------

    def _run_dir(self, graph_name: str, run_id: str) -> Path:
        return self._root / graph_name / "runs" / run_id

    def _states_dir(self, graph_name: str, run_id: str) -> Path:
        return self._run_dir(graph_name, run_id) / "states"

    def _logs_dir(self, graph_name: str, run_id: str) -> Path:
        return self._run_dir(graph_name, run_id) / "logs"


# ======================================================================
# Serialization helpers (plain dicts ↔ dataclasses)
# ======================================================================

def _graph_to_dict(g: Graph) -> dict:
    return {
        "name": g.name,
        "entry": g.entry,
        "description": g.description,
        "nodes": {
            name: {
                "name": n.name,
                "skill": n.skill,
                "backend": n.backend,
                "description": n.description,
                "transitions": [
                    {"target": t.target, "condition": t.condition}
                    for t in n.transitions
                ],
            }
            for name, n in g.nodes.items()
        },
    }


def _dict_to_graph(d: dict) -> Graph:
    nodes: dict[str, GraphNode] = {}
    for name, nd in d.get("nodes", {}).items():
        nodes[name] = GraphNode(
            name=nd["name"],
            skill=nd["skill"],
            backend=nd["backend"],
            description=nd.get("description", ""),
            transitions=[
                Transition(target=t["target"], condition=t.get("condition", "done"))
                for t in nd.get("transitions", [])
            ],
        )
    return Graph(
        name=d["name"],
        entry=d["entry"],
        nodes=nodes,
        description=d.get("description", ""),
    )


def _node_state_to_dict(s: NodeState) -> dict:
    return {
        "status": s.status.value,
        "session_id": s.session_id,
        "activated_by": s.activated_by,
        "input_data": s.input_data,
        "output": s.output,
        "error": s.error,
        "updated_at": s.updated_at,
    }


def _dict_to_node_state(d: dict) -> NodeState:
    return NodeState(
        status=NodeStatus(d.get("status", "idle")),
        session_id=d.get("session_id"),
        activated_by=d.get("activated_by"),
        input_data=d.get("input_data", ""),
        output=d.get("output", ""),
        error=d.get("error", ""),
        updated_at=d.get("updated_at", ""),
    )


def _run_to_dict(r: GraphRun) -> dict:
    return {
        "graph_name": r.graph_name,
        "run_id": r.run_id,
        "started_at": r.started_at,
        "status": r.status,
        "node_states": {
            name: _node_state_to_dict(s) for name, s in r.node_states.items()
        },
    }


def _dict_to_run(d: dict) -> GraphRun:
    return GraphRun(
        graph_name=d["graph_name"],
        run_id=d["run_id"],
        started_at=d["started_at"],
        status=d.get("status", "running"),
        node_states={
            name: _dict_to_node_state(s)
            for name, s in d.get("node_states", {}).items()
        },
    )

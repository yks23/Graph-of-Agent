"""GoA execution engine — poll-based state machine."""

from __future__ import annotations

import logging
import time
import threading
from datetime import datetime, timezone

from goa.backends import get_backend
from goa.models import Graph, GraphRun, NodeState, NodeStatus
from goa.storage import GoAStorage

log = logging.getLogger(__name__)


class GoAExecutor:
    """Drives graph execution by polling node state files."""

    def __init__(
        self,
        storage: GoAStorage,
        poll_interval: float = 2.0,
    ) -> None:
        self.storage = storage
        self.poll_interval = poll_interval
        self._stop_events: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, graph: Graph, *, blocking: bool = True) -> str:
        """Start a graph run. Returns run_id.

        If *blocking* is True (default), runs the poll loop in the current
        thread until all nodes are settled.  If False, launches a background
        thread and returns immediately.
        """
        errors = graph.validate()
        if errors:
            raise ValueError(f"Invalid graph: {'; '.join(errors)}")

        run = self.storage.create_run(graph.name)

        for node_name in graph.nodes:
            state = NodeState(status=NodeStatus.IDLE)
            self.storage.write_node_state(graph.name, run.run_id, node_name, state)

        entry_state = NodeState(status=NodeStatus.PENDING)
        self.storage.write_node_state(graph.name, run.run_id, graph.entry, entry_state)

        run.node_states = {
            n: self.storage.read_node_state(graph.name, run.run_id, n)
            for n in graph.nodes
        }
        self.storage.save_run(run)

        stop_event = threading.Event()
        key = f"{graph.name}/{run.run_id}"
        self._stop_events[key] = stop_event

        if blocking:
            self._run_loop(graph, run, stop_event)
        else:
            t = threading.Thread(
                target=self._run_loop,
                args=(graph, run, stop_event),
                daemon=True,
            )
            t.start()

        return run.run_id

    def stop(self, graph_name: str, run_id: str) -> None:
        """Signal a running graph to stop."""
        key = f"{graph_name}/{run_id}"
        evt = self._stop_events.get(key)
        if evt:
            evt.set()

        try:
            run = self.storage.load_run(graph_name, run_id)
            run.status = "stopped"
            self.storage.save_run(run)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------

    def _run_loop(
        self, graph: Graph, run: GraphRun, stop_event: threading.Event
    ) -> None:
        log.info("Run %s started for graph '%s'", run.run_id, graph.name)
        self.storage.append_log(
            graph.name, run.run_id, "_executor",
            f"Run started — entry node: {graph.entry}",
        )

        while not stop_event.is_set():
            still_active = self._poll_cycle(graph, run)
            if not still_active:
                break
            stop_event.wait(self.poll_interval)

        run = self.storage.load_run(graph.name, run.run_id)
        if run.status == "running":
            run.status = "completed"
            self.storage.save_run(run)

        self.storage.append_log(
            graph.name, run.run_id, "_executor",
            f"Run finished with status: {run.status}",
        )
        log.info("Run %s finished — %s", run.run_id, run.status)

        key = f"{graph.name}/{run.run_id}"
        self._stop_events.pop(key, None)

    def _poll_cycle(self, graph: Graph, run: GraphRun) -> bool:
        """Execute one poll cycle. Returns True if there are still active nodes."""
        pending_nodes: list[str] = []
        has_running = False

        for node_name in graph.nodes:
            state = self.storage.read_node_state(graph.name, run.run_id, node_name)
            if state.status == NodeStatus.PENDING:
                pending_nodes.append(node_name)
            elif state.status == NodeStatus.RUNNING:
                has_running = True

        for node_name in pending_nodes:
            self._execute_node(graph, run, node_name)

        still_active = False
        for node_name in graph.nodes:
            state = self.storage.read_node_state(graph.name, run.run_id, node_name)
            if state.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                still_active = True
                break

            if state.status == NodeStatus.DONE:
                has_outgoing = any(
                    t for t in graph.nodes[node_name].transitions
                    if t.condition == "done"
                )
                if has_outgoing:
                    already_activated = self._transitions_already_fired(
                        graph, run, node_name, "done"
                    )
                    if not already_activated:
                        self._activate_transitions(graph, run, node_name, "done")
                        still_active = True

        return still_active

    # ------------------------------------------------------------------
    # Node execution
    # ------------------------------------------------------------------

    def _execute_node(self, graph: Graph, run: GraphRun, node_name: str) -> None:
        node = graph.nodes[node_name]
        state = self.storage.read_node_state(graph.name, run.run_id, node_name)
        backend = get_backend(node.backend)

        prompt = self._build_prompt(node, state)

        state.status = NodeStatus.RUNNING
        self.storage.write_node_state(graph.name, run.run_id, node_name, state)
        self.storage.append_log(
            graph.name, run.run_id, node_name,
            f"RUNNING — backend={node.backend}",
        )

        workspace = str(self.storage.root.parent)
        result = backend.execute(prompt, workspace, state.session_id)

        state.session_id = result.session_id or state.session_id
        state.output = result.output
        state.updated_at = datetime.now(timezone.utc).isoformat()

        if result.success:
            state.status = NodeStatus.DONE
            state.error = ""
            self.storage.append_log(
                graph.name, run.run_id, node_name,
                f"DONE ({result.duration_sec:.1f}s)",
            )
        else:
            state.status = NodeStatus.ERROR
            state.error = result.error
            self.storage.append_log(
                graph.name, run.run_id, node_name,
                f"ERROR: {result.error}",
            )

        self.storage.write_node_state(graph.name, run.run_id, node_name, state)

        condition = "done" if result.success else "error"
        self._activate_transitions(graph, run, node_name, condition)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(node: "GraphNode", state: NodeState) -> str:
        """Build the prompt sent to the backend.

        Two cases:
          - First invocation (no session_id): full skill text + upstream input
          - Resume (has session_id): short activation prompt + upstream input
        """
        from goa.models import GraphNode  # deferred to avoid circular at module level

        has_upstream = bool(state.input_data)
        is_resume = bool(state.session_id)

        if is_resume:
            parts = [f"继续执行任务 (节点: {node.name})。"]
            if has_upstream:
                parts.append(
                    f"上游节点 '{state.activated_by}' 传入数据:\n"
                    f"---\n{state.input_data}\n---"
                )
            else:
                parts.append("无新的上游数据。")
            return "\n\n".join(parts)

        parts = [node.skill]
        if has_upstream:
            parts.append(
                f"\n\n## 上游输入\n"
                f"激活方: {state.activated_by}\n"
                f"传入数据:\n{state.input_data}"
            )
        return "".join(parts)

    # ------------------------------------------------------------------
    # Transition activation
    # ------------------------------------------------------------------

    def _activate_transitions(
        self, graph: Graph, run: GraphRun, source_name: str, condition: str
    ) -> None:
        node = graph.nodes[source_name]
        source_state = self.storage.read_node_state(
            graph.name, run.run_id, source_name
        )

        for t in node.transitions:
            if t.condition != condition:
                continue
            target_state = self.storage.read_node_state(
                graph.name, run.run_id, t.target
            )
            if target_state.status in (NodeStatus.RUNNING, NodeStatus.PENDING):
                continue

            target_state.status = NodeStatus.PENDING
            target_state.activated_by = source_name
            target_state.input_data = source_state.output
            self.storage.write_node_state(
                graph.name, run.run_id, t.target, target_state
            )
            self.storage.append_log(
                graph.name, run.run_id, t.target,
                f"PENDING — activated by '{source_name}' (condition={condition})"
                f" | input_data={len(source_state.output)} chars",
            )

    def _transitions_already_fired(
        self, graph: Graph, run: GraphRun, source_name: str, condition: str
    ) -> bool:
        """Check if all downstream targets for this condition are already beyond IDLE."""
        node = graph.nodes[source_name]
        for t in node.transitions:
            if t.condition != condition:
                continue
            target_state = self.storage.read_node_state(
                graph.name, run.run_id, t.target
            )
            if target_state.status == NodeStatus.IDLE:
                return False
        return True

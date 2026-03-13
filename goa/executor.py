"""GoA execution engine — poll-based state machine with inbox messaging."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone

from goa.backends import get_backend
from goa.models import (
    Graph,
    GraphNode,
    GraphRun,
    InboxMessage,
    NodeState,
    NodeStatus,
    Transition,
)
from goa.storage import GoAStorage

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GoAExecutor:
    """Drives graph execution by polling node state files and inboxes."""

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
        """Start a graph run. Returns run_id."""
        errors = graph.validate()
        if errors:
            raise ValueError(f"Invalid graph: {'; '.join(errors)}")

        run = self.storage.create_run(graph.name)

        for node_name in graph.nodes:
            state = NodeState(status=NodeStatus.IDLE)
            self.storage.write_node_state(graph.name, run.run_id, node_name, state)

        self.storage.drop_inbox_message(
            graph.name,
            run.run_id,
            graph.entry,
            InboxMessage(
                source="_system",
                condition="start",
                data="",
                timestamp=_now(),
            ),
        )

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
        self._process_inboxes(graph, run)

        for node_name in graph.nodes:
            state = self.storage.read_node_state(graph.name, run.run_id, node_name)
            if state.status == NodeStatus.PENDING:
                self._execute_node(graph, run, node_name)

        for node_name in graph.nodes:
            state = self.storage.read_node_state(graph.name, run.run_id, node_name)
            if state.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                return True
            if self.storage.read_inbox(graph.name, run.run_id, node_name):
                return True

        return False

    # ------------------------------------------------------------------
    # Inbox processing
    # ------------------------------------------------------------------

    def _process_inboxes(self, graph: Graph, run: GraphRun) -> None:
        for node_name in graph.nodes:
            state = self.storage.read_node_state(graph.name, run.run_id, node_name)
            if state.status in (NodeStatus.RUNNING, NodeStatus.PENDING):
                continue

            messages = self.storage.read_inbox(graph.name, run.run_id, node_name)
            if not messages:
                continue

            if len(messages) == 1:
                msg = messages[0]
                state.activated_by = msg.source
                state.input_data = msg.data
            else:
                sources: list[str] = []
                parts: list[str] = []
                for msg in messages:
                    sources.append(msg.source)
                    if msg.data:
                        parts.append(f"[From {msg.source} ({msg.condition})]:\n{msg.data}")
                state.activated_by = ", ".join(sources)
                state.input_data = "\n\n".join(parts)

            state.status = NodeStatus.PENDING
            self.storage.write_node_state(graph.name, run.run_id, node_name, state)
            self.storage.clear_inbox(graph.name, run.run_id, node_name)

            self.storage.append_log(
                graph.name, run.run_id, node_name,
                f"PENDING — inbox consumed: {len(messages)} msg(s) "
                f"from [{state.activated_by}]",
            )

    # ------------------------------------------------------------------
    # Node execution
    # ------------------------------------------------------------------

    def _execute_node(self, graph: Graph, run: GraphRun, node_name: str) -> None:
        node = graph.nodes[node_name]
        state = self.storage.read_node_state(graph.name, run.run_id, node_name)
        backend = get_backend(node.backend)

        prompt = self._build_prompt(graph, run, node, state)

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
        state.updated_at = _now()

        if not result.success:
            state.status = NodeStatus.ERROR
            state.error = result.error
            self.storage.write_node_state(graph.name, run.run_id, node_name, state)
            self.storage.append_log(
                graph.name, run.run_id, node_name,
                f"ERROR (backend failure): {result.error}",
            )
            return

        state.status = NodeStatus.DONE
        state.error = ""
        self.storage.append_log(
            graph.name, run.run_id, node_name,
            f"DONE ({result.duration_sec:.1f}s)",
        )
        self.storage.write_node_state(graph.name, run.run_id, node_name, state)

        self._dispatch_calls(graph, run, node, state)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        graph: Graph,
        run: GraphRun,
        node: GraphNode,
        state: NodeState,
    ) -> str:
        is_resume = bool(state.session_id)
        workspace = str(self.storage.root.parent)

        if is_resume:
            return self._build_resume_prompt(graph, node, state, workspace)
        return self._build_first_prompt(graph, run, node, state, workspace)

    def _build_first_prompt(
        self,
        graph: Graph,
        run: GraphRun,
        node: GraphNode,
        state: NodeState,
        workspace: str,
    ) -> str:
        sections: list[str] = []

        # 1. Skill
        sections.append(node.skill)

        # 2. Workspace
        sections.append(
            f"## Workspace\n"
            f"- Working directory: {workspace}\n"
            f"- GoA data directory: {workspace}/.goa/\n"
            f"- Graph: {graph.name}\n"
            f"- Run ID: {run.run_id}\n"
            f"- Node: {node.name} (backend: {node.backend})"
        )

        # 3. Workflow
        workflow_lines = ["## Workflow"]
        workflow_lines.append(f"Graph \"{graph.name}\": {graph.description}")
        workflow_lines.append(f"Entry node: {graph.entry}")
        workflow_lines.append("")
        workflow_lines.append("Nodes in this graph:")
        for n_name, n in graph.nodes.items():
            marker = " ← YOU" if n_name == node.name else ""
            workflow_lines.append(
                f"  [{n_name}] ({n.backend}) — {n.description or n.skill[:60]}{marker}"
            )
            for t in n.transitions:
                workflow_lines.append(f"    ──{t.condition}──> {t.target}")
        sections.append("\n".join(workflow_lines))

        # 4. Upstream input
        if state.input_data:
            sections.append(
                f"## Upstream Input\n"
                f"Activated by: {state.activated_by}\n"
                f"Data:\n{state.input_data}"
            )

        # 5. Call
        sections.append(self._build_call_section(node))

        # 6. Constraints
        sections.append(self._build_constraints_section(node))

        return "\n\n".join(sections)

    def _build_resume_prompt(
        self,
        graph: Graph,
        node: GraphNode,
        state: NodeState,
        workspace: str,
    ) -> str:
        parts: list[str] = []
        parts.append(
            f"You are being re-activated as node \"{node.name}\" in graph "
            f"\"{graph.name}\" (workspace: {workspace})."
        )

        if state.input_data:
            parts.append(
                f"## Upstream Input\n"
                f"Activated by: {state.activated_by}\n"
                f"Data:\n{state.input_data}"
            )

        parts.append(self._build_call_section(node))
        parts.append(self._build_constraints_section(node))

        return "\n\n".join(parts)

    @staticmethod
    def _build_call_section(node: GraphNode) -> str:
        lines = [
            "## Call",
            "When your task is complete, you MUST output exactly one fenced "
            "block to activate downstream node(s). Format:",
            "",
            "```GOA_CALL",
            '[{"target": "<node_name>", "condition": "<condition>", '
            '"data": "<message to pass>"}]',
            "```",
            "",
            "Rules:",
            "- The block MUST be valid JSON: an array of objects.",
            "- Each object: target (string), condition (string), data (string).",
            "- target and condition MUST exactly match one of your declared "
            "transitions below.",
            "- You MUST NOT call targets that are not in your transitions.",
            "- data is the message passed to the target node — include a "
            "concise summary of your work or findings.",
            "- For terminal nodes (no transitions): output an empty array `[]`.",
            "",
            "Your declared transitions:",
        ]

        if node.transitions:
            for t in node.transitions:
                lines.append(f'  - target="{t.target}", condition="{t.condition}"')
        else:
            lines.append("  (none — you are a terminal node, output `[]`)")

        lines.append("")
        lines.append("Example:")
        if node.transitions:
            first = node.transitions[0]
            lines.append("```GOA_CALL")
            lines.append(json.dumps(
                [{"target": first.target, "condition": first.condition,
                  "data": "Summary of what I did..."}],
                ensure_ascii=False,
            ))
            lines.append("```")
        else:
            lines.append("```GOA_CALL")
            lines.append("[]")
            lines.append("```")

        return "\n".join(lines)

    @staticmethod
    def _build_constraints_section(node: GraphNode) -> str:
        lines = [
            "## Constraints",
            "- Focus ONLY on your Skill. Do not do work assigned to other nodes.",
            "- You MUST emit exactly one GOA_CALL block when done.",
            "- You MUST only call targets declared in your transitions. "
            "Any other target will be rejected.",
            "- Do NOT modify anything under the .goa/ directory.",
            "- Your working directory is the project workspace root.",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Transition dispatch — parse GOA_CALL, strict validation
    # ------------------------------------------------------------------

    def _dispatch_calls(
        self,
        graph: Graph,
        run: GraphRun,
        node: GraphNode,
        state: NodeState,
    ) -> None:
        """Parse GOA_CALL from output. Strict: must exist, must match graph."""
        calls = _parse_goa_calls(state.output)

        if not node.transitions:
            if calls:
                self.storage.append_log(
                    graph.name, run.run_id, node.name,
                    f"CALL ignored: terminal node emitted {len(calls)} call(s)",
                )
            return

        if not calls:
            self.storage.append_log(
                graph.name, run.run_id, node.name,
                "ERROR: no GOA_CALL block found in output — "
                "node has transitions but agent did not emit a call",
            )
            state.status = NodeStatus.ERROR
            state.error = "No GOA_CALL block in output"
            self.storage.write_node_state(
                graph.name, run.run_id, node.name, state
            )
            return

        valid_edges: dict[str, str] = {
            (t.target, t.condition): True for t in node.transitions
        }
        valid_targets = {t.target for t in node.transitions}

        for call in calls:
            target = call.get("target", "")
            condition = call.get("condition", "done")
            data = call.get("data", "")

            if target not in valid_targets:
                self.storage.append_log(
                    graph.name, run.run_id, node.name,
                    f"CALL rejected: target '{target}' is not in "
                    f"declared transitions {sorted(valid_targets)}",
                )
                continue

            if (target, condition) not in valid_edges:
                valid_conditions = [
                    t.condition for t in node.transitions if t.target == target
                ]
                self.storage.append_log(
                    graph.name, run.run_id, node.name,
                    f"CALL rejected: edge ({target}, {condition}) does not exist. "
                    f"Valid conditions for '{target}': {valid_conditions}",
                )
                continue

            msg = InboxMessage(
                source=node.name,
                condition=condition,
                data=data,
                timestamp=_now(),
            )
            self.storage.drop_inbox_message(
                graph.name, run.run_id, target, msg
            )
            self.storage.append_log(
                graph.name, run.run_id, target,
                f"INBOX ← from '{node.name}' "
                f"(condition={condition}, {len(data)} chars)",
            )


# ======================================================================
# GOA_CALL parser
# ======================================================================

_GOA_CALL_PATTERN = re.compile(
    r"```GOA_CALL\s*\n(.*?)\n\s*```",
    re.DOTALL,
)


def _parse_goa_calls(output: str) -> list[dict]:
    """Extract GOA_CALL JSON blocks from agent output."""
    matches = _GOA_CALL_PATTERN.findall(output)
    if not matches:
        return []

    calls: list[dict] = []
    for raw in matches:
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Failed to parse GOA_CALL block: %s", raw[:200])
            continue
        if isinstance(parsed, list):
            calls.extend(parsed)
        elif isinstance(parsed, dict):
            calls.append(parsed)

    return calls

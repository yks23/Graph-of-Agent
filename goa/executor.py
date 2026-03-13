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

GOA_CALL_FENCE = "GOA_CALL"


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
        """Single poll iteration.

        Phase 1: consume inboxes → promote IDLE/DONE/ERROR nodes to PENDING
        Phase 2: execute all PENDING nodes (serial)
        Phase 3: determine if graph is still active
        """
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
        """For each node: if not RUNNING/PENDING and has inbox messages,
        consume them and promote to PENDING."""
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

        self._dispatch_transitions(graph, run, node, state, result.success)

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
        """Build the full prompt for a node invocation."""
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
        """First invocation: full context injection."""
        sections: list[str] = []

        # ---- Section 1: Skill ----
        sections.append(node.skill)

        # ---- Section 2: Workspace ----
        sections.append(
            f"## Workspace\n"
            f"- Working directory: {workspace}\n"
            f"- GoA data directory: {workspace}/.goa/\n"
            f"- Graph: {graph.name}\n"
            f"- Run ID: {run.run_id}\n"
            f"- Node: {node.name} (backend: {node.backend})"
        )

        # ---- Section 3: Workflow ----
        workflow_lines = [f"## Workflow"]
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

        # ---- Section 4: Upstream input ----
        if state.input_data:
            sections.append(
                f"## Upstream Input\n"
                f"Activated by: {state.activated_by}\n"
                f"Data:\n{state.input_data}"
            )

        # ---- Section 5: Call mechanism ----
        sections.append(self._build_call_section(node))

        # ---- Section 6: Constraints ----
        sections.append(
            "## Constraints\n"
            "- Stay focused on your Skill described above. Do not take on work "
            "assigned to other nodes.\n"
            "- When your task is complete, you MUST emit a GOA_CALL block "
            "(see Call section) so the next node can be activated.\n"
            "- If you cannot complete the task, emit a GOA_CALL with "
            "condition=\"error\" to trigger the error path.\n"
            "- Do NOT attempt to run or modify the GoA framework itself.\n"
            "- Your working directory is the project workspace, not the .goa/ "
            "data directory."
        )

        return "\n\n".join(sections)

    def _build_resume_prompt(
        self,
        graph: Graph,
        node: GraphNode,
        state: NodeState,
        workspace: str,
    ) -> str:
        """Resume invocation: short activation with upstream data."""
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

        parts.append(
            "## Constraints\n"
            "- Continue from where you left off.\n"
            "- When done, emit a GOA_CALL block to activate the next node."
        )

        return "\n\n".join(parts)

    @staticmethod
    def _build_call_section(node: GraphNode) -> str:
        """Build the Call section that teaches the agent how to signal transitions."""
        lines = [
            "## Call",
            "When you are done, you MUST output a fenced block to signal which "
            "node(s) to activate next. Format:",
            "",
            "```GOA_CALL",
            '[{"target": "<node_name>", "condition": "<done|error>", '
            '"data": "<message to pass>"}]',
            "```",
            "",
            "Rules:",
            "- The block must be valid JSON: an array of objects.",
            "- Each object needs: target (string), condition (string), "
            "data (string).",
            "- You may call multiple targets in one block.",
            '- If your task succeeded, use condition="done".',
            '- If your task failed, use condition="error".',
            "- The data field is passed as input to the target node — include "
            "a useful summary of your work.",
            "- If you emit no GOA_CALL block, the executor falls back to "
            "default transitions based on exit status.",
            "",
            "Your available transitions:",
        ]

        if node.transitions:
            for t in node.transitions:
                lines.append(f'  - target="{t.target}", condition="{t.condition}"')
        else:
            lines.append("  (none — you are a terminal node)")

        lines.append("")
        lines.append("Example:")
        if node.transitions:
            first = node.transitions[0]
            lines.append("```GOA_CALL")
            lines.append(json.dumps(
                [{"target": first.target, "condition": first.condition,
                  "data": "Task completed. Summary: ..."}],
                ensure_ascii=False,
            ))
            lines.append("```")
        else:
            lines.append("```GOA_CALL")
            lines.append("[]")
            lines.append("```")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Transition dispatch — parse GOA_CALL from output, fallback to default
    # ------------------------------------------------------------------

    def _dispatch_transitions(
        self,
        graph: Graph,
        run: GraphRun,
        node: GraphNode,
        state: NodeState,
        success: bool,
    ) -> None:
        """Parse GOA_CALL blocks from output. Fall back to default transitions."""
        calls = _parse_goa_calls(state.output)

        if calls:
            valid_targets = {t.target for t in node.transitions}
            for call in calls:
                target = call.get("target", "")
                condition = call.get("condition", "done")
                data = call.get("data", state.output)

                if target not in graph.nodes:
                    self.storage.append_log(
                        graph.name, run.run_id, node.name,
                        f"CALL ignored: unknown target '{target}'",
                    )
                    continue

                if target not in valid_targets:
                    self.storage.append_log(
                        graph.name, run.run_id, node.name,
                        f"CALL warning: '{target}' not in declared transitions, "
                        f"sending anyway",
                    )

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
                    f"INBOX ← GOA_CALL from '{node.name}' "
                    f"(condition={condition}, {len(data)} chars)",
                )
        else:
            self._send_default_transitions(graph, run, node, state, success)

    def _send_default_transitions(
        self,
        graph: Graph,
        run: GraphRun,
        node: GraphNode,
        state: NodeState,
        success: bool,
    ) -> None:
        """Fallback: fire transitions based on success/error condition matching."""
        condition = "done" if success else "error"

        for t in node.transitions:
            if t.condition != condition:
                continue

            msg = InboxMessage(
                source=node.name,
                condition=condition,
                data=state.output,
                timestamp=_now(),
            )
            self.storage.drop_inbox_message(
                graph.name, run.run_id, t.target, msg
            )
            self.storage.append_log(
                graph.name, run.run_id, t.target,
                f"INBOX ← default transition from '{node.name}' "
                f"(condition={condition}, {len(state.output)} chars)",
            )


# ======================================================================
# GOA_CALL parser
# ======================================================================

_GOA_CALL_PATTERN = re.compile(
    r"```GOA_CALL\s*\n(.*?)\n\s*```",
    re.DOTALL,
)


def _parse_goa_calls(output: str) -> list[dict]:
    """Extract GOA_CALL JSON blocks from agent output.

    Returns a list of call dicts, or empty list if none found.
    """
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

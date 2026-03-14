# GoA — Graph of Agents

Orchestrate multiple AI coding agents as a directed graph. Each node in the graph represents an agent invocation with a specific skill, backend, and transition rules.

## Quick Start

```bash
pip install -e .

# List available backends
graph backends

# Import a graph definition
graph import my-workflow.json

# List all graphs
graph list

# Show graph structure
graph show my-workflow

# Run a graph
graph run my-workflow

# Check status
graph status my-workflow --logs

# Launch TUI editor
graph create

# Start Web UI
graph serve
```

## Architecture

```
goa/
    models.py          Pure dataclasses (Graph, GraphNode, Transition, NodeState, ...)
    storage.py         File-system persistence (.goa/ directory)
    executor.py        Poll-based execution engine
    cli.py             CLI entry point (graph command)
    backends/
        base.py        AgentBackend ABC
        cursor.py      Cursor Agent backend
        codex.py       OpenAI Codex CLI backend
        claude_code.py Claude Code CLI backend
    ui/
        tui.py         Rich-based interactive graph editor
        web.py         HTTP API server
        static/
            index.html Single-page frontend app
    skills/
        constructor.md Conversational graph builder skill
```

## Backends

| Backend | CLI Tool | Session Resume |
|---------|----------|---------------|
| `cursor` | `cursor-agent` | `--resume SID` |
| `codex` | `codex` | prompt injection |
| `claude_code` | `claude` | `--resume SID` |

## Graph Definition Format

```json
{
  "name": "my-workflow",
  "description": "Example workflow",
  "entry": "analyzer",
  "nodes": {
    "analyzer": {
      "name": "analyzer",
      "skill": "Analyze the codebase and identify issues",
      "backend": "cursor",
      "description": "Code analyzer",
      "transitions": [
        {"target": "implementer", "condition": "done"},
        {"target": "debugger", "condition": "error"}
      ]
    }
  }
}
```

## How It Works

1. **Define** a graph with nodes (agents) and transitions (edges)
2. **Run** the graph — the executor activates the entry node
3. **Poll** — the engine checks node state files every 2 seconds
4. **Execute** — PENDING nodes get dispatched to their backend
5. **Transition** — DONE/ERROR nodes activate downstream nodes per transition rules
6. **Complete** — when all nodes settle, the run finishes

State is persisted as JSON files under `.goa/{graph}/runs/{run_id}/`, making the system fully observable and debuggable.

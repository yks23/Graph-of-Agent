"""CLI entry point — the `graph` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

console = Console()


def _get_storage(args: argparse.Namespace):
    from goa.storage import GoAStorage
    workspace = getattr(args, "workspace", None) or "."
    return GoAStorage(Path(workspace))


# ------------------------------------------------------------------
# Sub-commands
# ------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> None:
    storage = _get_storage(args)
    graphs = storage.list_graphs()
    if not graphs:
        console.print("[dim]No graphs found.[/dim]")
        return
    table = Table(title="GoA Graphs")
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    table.add_column("Nodes", justify="right")
    table.add_column("Entry", style="green")
    for name in graphs:
        g = storage.load_graph(name)
        table.add_row(name, g.description, str(len(g.nodes)), g.entry)
    console.print(table)


def cmd_show(args: argparse.Namespace) -> None:
    storage = _get_storage(args)
    try:
        g = storage.load_graph(args.name)
    except FileNotFoundError:
        console.print(f"[red]Graph '{args.name}' not found.[/red]")
        sys.exit(1)

    tree = Tree(f"[bold]{g.name}[/bold]  [dim]{g.description}[/dim]")
    for name, node in g.nodes.items():
        prefix = "[bold green][entry][/bold green] " if name == g.entry else ""
        branch = tree.add(f"{prefix}{name} [dim]({node.backend})[/dim]")
        if node.description:
            branch.add(f"[dim]{node.description}[/dim]")
        for t in node.transitions:
            branch.add(f"──{t.condition}──> {t.target}")

    console.print(Panel(tree, title=f"Graph: {g.name}"))


def cmd_create(args: argparse.Namespace) -> None:
    from goa.ui.tui import run_tui_editor
    storage = _get_storage(args)
    run_tui_editor(storage)


def cmd_run(args: argparse.Namespace) -> None:
    from goa.executor import GoAExecutor
    storage = _get_storage(args)
    try:
        graph = storage.load_graph(args.name)
    except FileNotFoundError:
        console.print(f"[red]Graph '{args.name}' not found.[/red]")
        sys.exit(1)

    executor = GoAExecutor(storage)
    console.print(f"[bold]Starting graph '{args.name}'...[/bold]")
    try:
        run_id = executor.start(graph, blocking=True)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return
    console.print(f"[green]Run completed: {run_id}[/green]")


def cmd_status(args: argparse.Namespace) -> None:
    storage = _get_storage(args)
    run_id = args.run_id
    if not run_id:
        run_id = storage.latest_run_id(args.name)
    if not run_id:
        console.print(f"[dim]No runs found for '{args.name}'.[/dim]")
        return

    try:
        run = storage.load_run(args.name, run_id)
    except FileNotFoundError:
        console.print(f"[red]Run '{run_id}' not found.[/red]")
        sys.exit(1)

    try:
        graph = storage.load_graph(args.name)
    except FileNotFoundError:
        graph = None

    table = Table(title=f"Run {run.run_id} — {run.status}")
    table.add_column("Node", style="cyan")
    table.add_column("Status")
    table.add_column("Activated By")
    table.add_column("Updated")

    node_names = list(graph.nodes.keys()) if graph else []
    if not node_names:
        node_names = list(run.node_states.keys())

    status_styles = {
        "idle": "dim",
        "pending": "yellow",
        "running": "blue bold",
        "done": "green",
        "error": "red bold",
    }
    for name in node_names:
        state = storage.read_node_state(args.name, run_id, name)
        style = status_styles.get(state.status.value, "")
        table.add_row(
            name,
            f"[{style}]{state.status.value}[/{style}]",
            state.activated_by or "",
            state.updated_at[:19] if state.updated_at else "",
        )

    console.print(table)

    if args.logs:
        for name in node_names:
            log_text = storage.read_log(args.name, run_id, name)
            if log_text:
                console.print(Panel(log_text.strip(), title=f"Log: {name}"))
        executor_log = storage.read_log(args.name, run_id, "_executor")
        if executor_log:
            console.print(Panel(executor_log.strip(), title="Log: _executor"))


def cmd_stop(args: argparse.Namespace) -> None:
    from goa.executor import GoAExecutor
    storage = _get_storage(args)
    run_id = args.run_id
    if not run_id:
        run_id = storage.latest_run_id(args.name)
    if not run_id:
        console.print(f"[dim]No active run for '{args.name}'.[/dim]")
        return
    executor = GoAExecutor(storage)
    executor.stop(args.name, run_id)
    console.print(f"[yellow]Stopped run {run_id}[/yellow]")


def cmd_delete(args: argparse.Namespace) -> None:
    storage = _get_storage(args)
    storage.delete_graph(args.name)
    console.print(f"[red]Deleted graph '{args.name}'[/red]")


def cmd_backends(args: argparse.Namespace) -> None:
    from goa.backends import list_backends
    table = Table(title="Agent Backends")
    table.add_column("Name", style="cyan")
    table.add_column("Available")
    table.add_column("Install Hint")
    for b in list_backends():
        avail = "[green]Yes[/green]" if b["available"] else "[red]No[/red]"
        table.add_row(b["name"], avail, b["hint"])
    console.print(table)


def cmd_construct(args: argparse.Namespace) -> None:
    storage = _get_storage(args)
    skill_path = Path(__file__).parent / "skills" / "constructor.md"
    if not skill_path.exists():
        console.print("[red]Constructor skill file not found.[/red]")
        sys.exit(1)

    skill_text = skill_path.read_text(encoding="utf-8")

    backend_name = args.backend or "cursor"
    from goa.backends import get_backend
    try:
        backend = get_backend(backend_name)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    if not backend.is_available():
        console.print(
            f"[red]Backend '{backend_name}' is not available. "
            f"{backend.install_hint()}[/red]"
        )
        sys.exit(1)

    workspace = str(Path(getattr(args, "workspace", None) or ".").resolve())
    console.print(f"[bold]Launching Constructor with '{backend_name}' backend...[/bold]")
    result = backend.execute(skill_text, workspace)
    if result.success:
        console.print("[green]Constructor completed.[/green]")
        console.print(result.output)
    else:
        console.print(f"[red]Constructor failed: {result.error}[/red]")


def cmd_import(args: argparse.Namespace) -> None:
    """Import a graph from a JSON file."""
    storage = _get_storage(args)
    path = Path(args.file)
    if not path.exists():
        console.print(f"[red]File not found: {path}[/red]")
        sys.exit(1)

    from goa.storage import _dict_to_graph
    data = json.loads(path.read_text(encoding="utf-8"))
    graph = _dict_to_graph(data)
    errors = graph.validate()
    if errors:
        console.print("[red]Validation errors:[/red]")
        for e in errors:
            console.print(f"  - {e}")
        sys.exit(1)
    storage.save_graph(graph)
    console.print(f"[green]Imported graph '{graph.name}'[/green]")


def cmd_export(args: argparse.Namespace) -> None:
    """Export a graph to a JSON file."""
    storage = _get_storage(args)
    try:
        graph = storage.load_graph(args.name)
    except FileNotFoundError:
        console.print(f"[red]Graph '{args.name}' not found.[/red]")
        sys.exit(1)

    from goa.storage import _graph_to_dict
    data = _graph_to_dict(graph)
    output = args.output or f"{args.name}.json"
    Path(output).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    console.print(f"[green]Exported to {output}[/green]")


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the GoA Web UI server."""
    from goa.ui.web import run_server
    storage = _get_storage(args)
    host = args.host or "127.0.0.1"
    port = args.port or 7862
    console.print(f"[bold]Starting GoA Web UI at http://{host}:{port}[/bold]")
    run_server(storage, host=host, port=port)


# ------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graph",
        description="GoA — Graph of Agents CLI",
    )
    parser.add_argument(
        "--workspace", "-w",
        default=None,
        help="Workspace directory (default: current directory)",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List all GoA graphs")

    p_show = sub.add_parser("show", help="Show graph structure")
    p_show.add_argument("name")

    sub.add_parser("create", help="Interactive TUI graph editor")

    p_run = sub.add_parser("run", help="Run a graph")
    p_run.add_argument("name")

    p_status = sub.add_parser("status", help="Show run status")
    p_status.add_argument("name")
    p_status.add_argument("--run-id", default=None)
    p_status.add_argument("--logs", action="store_true", help="Show node logs")

    p_stop = sub.add_parser("stop", help="Stop a running graph")
    p_stop.add_argument("name")
    p_stop.add_argument("--run-id", default=None)

    p_delete = sub.add_parser("delete", help="Delete a graph")
    p_delete.add_argument("name")

    sub.add_parser("backends", help="List agent backends and availability")

    p_construct = sub.add_parser("construct", help="Launch Constructor (conversational graph builder)")
    p_construct.add_argument("--backend", default=None, help="Backend to use (default: cursor)")

    p_import = sub.add_parser("import", help="Import graph from JSON file")
    p_import.add_argument("file", help="Path to JSON file")

    p_export = sub.add_parser("export", help="Export graph to JSON file")
    p_export.add_argument("name")
    p_export.add_argument("--output", "-o", default=None, help="Output file path")

    p_serve = sub.add_parser("serve", help="Start the GoA Web UI server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=7862)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return

    commands = {
        "list": cmd_list,
        "show": cmd_show,
        "create": cmd_create,
        "run": cmd_run,
        "status": cmd_status,
        "stop": cmd_stop,
        "delete": cmd_delete,
        "backends": cmd_backends,
        "construct": cmd_construct,
        "import": cmd_import,
        "export": cmd_export,
        "serve": cmd_serve,
    }

    fn = commands.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

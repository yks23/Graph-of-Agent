"""Rich-based TUI for interactive graph building."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.tree import Tree

from goa.backends import list_backends
from goa.models import Graph, GraphNode, Transition
from goa.storage import GoAStorage

console = Console()


def run_tui_editor(storage: GoAStorage, existing: Graph | None = None) -> Graph | None:
    """Launch the interactive graph editor. Returns the saved graph or None."""
    console.print(
        Panel(
            "[bold]GoA — Interactive Graph Editor[/bold]\n"
            "Build an agent collaboration graph step by step.",
            style="blue",
        )
    )

    if existing:
        graph = existing
        console.print(f"Editing graph: [cyan]{graph.name}[/cyan]")
    else:
        name = Prompt.ask("[bold]Graph name[/bold]").strip()
        if not name:
            console.print("[red]Name cannot be empty.[/red]")
            return None
        desc = Prompt.ask("[bold]Description[/bold] (optional)", default="")
        graph = Graph(name=name, entry="", nodes={}, description=desc)

    while True:
        _print_menu()
        choice = Prompt.ask("Choice", choices=["a", "b", "c", "d", "e", "f", "g", "h", "q"])

        if choice == "a":
            _add_node(graph)
        elif choice == "b":
            _remove_node(graph)
        elif choice == "c":
            _add_transition(graph)
        elif choice == "d":
            _remove_transition(graph)
        elif choice == "e":
            _set_entry(graph)
        elif choice == "f":
            _preview(graph)
        elif choice == "g":
            _validate(graph)
        elif choice == "h":
            _save(graph, storage)
        elif choice == "q":
            if Confirm.ask("Quit without saving?", default=False):
                return None
            continue

    return graph


def _print_menu() -> None:
    console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("(a)", "Add node")
    table.add_row("(b)", "Remove node")
    table.add_row("(c)", "Add transition (edge)")
    table.add_row("(d)", "Remove transition")
    table.add_row("(e)", "Set entry node")
    table.add_row("(f)", "Preview graph")
    table.add_row("(g)", "Validate")
    table.add_row("(h)", "Save")
    table.add_row("(q)", "Quit")
    console.print(table)


def _add_node(graph: Graph) -> None:
    name = Prompt.ask("  Node name").strip()
    if not name:
        console.print("  [red]Name cannot be empty.[/red]")
        return
    if name in graph.nodes:
        console.print(f"  [red]Node '{name}' already exists.[/red]")
        return

    backends = list_backends()
    backend_names = [b["name"] for b in backends]
    console.print(f"  Available backends: {', '.join(backend_names)}")
    backend = Prompt.ask("  Backend", choices=backend_names, default="cursor")

    desc = Prompt.ask("  Description (optional)", default="")

    console.print("  Enter skill text (press Enter twice to finish):")
    lines: list[str] = []
    while True:
        line = Prompt.ask("  ", default="")
        if line == "" and lines and lines[-1] == "":
            lines.pop()
            break
        lines.append(line)
    skill = "\n".join(lines)

    node = GraphNode(
        name=name,
        skill=skill,
        backend=backend,
        description=desc,
    )
    graph.nodes[name] = node

    if len(graph.nodes) == 1 and not graph.entry:
        graph.entry = name
        console.print(f"  [green]Auto-set entry node to '{name}'[/green]")

    console.print(f"  [green]Added node '{name}'[/green]")


def _remove_node(graph: Graph) -> None:
    if not graph.nodes:
        console.print("  [dim]No nodes to remove.[/dim]")
        return
    _show_nodes(graph)
    name = Prompt.ask("  Remove node").strip()
    if name not in graph.nodes:
        console.print(f"  [red]Node '{name}' not found.[/red]")
        return

    del graph.nodes[name]

    for node in graph.nodes.values():
        node.transitions = [t for t in node.transitions if t.target != name]

    if graph.entry == name:
        graph.entry = ""
        console.print("  [yellow]Warning: entry node was removed, please set a new one.[/yellow]")

    console.print(f"  [red]Removed node '{name}'[/red]")


def _add_transition(graph: Graph) -> None:
    if len(graph.nodes) < 2:
        console.print("  [dim]Need at least 2 nodes to create a transition.[/dim]")
        return
    _show_nodes(graph)
    source = Prompt.ask("  Source node").strip()
    if source not in graph.nodes:
        console.print(f"  [red]Node '{source}' not found.[/red]")
        return

    target = Prompt.ask("  Target node").strip()
    if target not in graph.nodes:
        console.print(f"  [red]Node '{target}' not found.[/red]")
        return

    condition = Prompt.ask("  Condition", default="done")
    graph.nodes[source].transitions.append(
        Transition(target=target, condition=condition)
    )
    console.print(f"  [green]Added: {source} ──{condition}──> {target}[/green]")


def _remove_transition(graph: Graph) -> None:
    all_transitions: list[tuple[str, int, Transition]] = []
    for name, node in graph.nodes.items():
        for i, t in enumerate(node.transitions):
            all_transitions.append((name, i, t))

    if not all_transitions:
        console.print("  [dim]No transitions to remove.[/dim]")
        return

    table = Table(title="Transitions")
    table.add_column("#", style="cyan")
    table.add_column("Source")
    table.add_column("Condition")
    table.add_column("Target")
    for idx, (src, _, t) in enumerate(all_transitions):
        table.add_row(str(idx), src, t.condition, t.target)
    console.print(table)

    choice = Prompt.ask("  Remove # ").strip()
    try:
        idx = int(choice)
        src_name, trans_idx, _ = all_transitions[idx]
        del graph.nodes[src_name].transitions[trans_idx]
        console.print(f"  [red]Removed transition #{idx}[/red]")
    except (ValueError, IndexError):
        console.print("  [red]Invalid selection.[/red]")


def _set_entry(graph: Graph) -> None:
    if not graph.nodes:
        console.print("  [dim]No nodes defined yet.[/dim]")
        return
    _show_nodes(graph)
    name = Prompt.ask("  Entry node").strip()
    if name not in graph.nodes:
        console.print(f"  [red]Node '{name}' not found.[/red]")
        return
    graph.entry = name
    console.print(f"  [green]Entry set to '{name}'[/green]")


def _preview(graph: Graph) -> None:
    if not graph.nodes:
        console.print("  [dim]Graph is empty.[/dim]")
        return

    tree = Tree(f"[bold]{graph.name}[/bold]")
    for name, node in graph.nodes.items():
        prefix = "[bold green][entry][/bold green] " if name == graph.entry else ""
        branch = tree.add(f"{prefix}{name} [dim]({node.backend})[/dim]")
        for t in node.transitions:
            branch.add(f"──{t.condition}──> {t.target}")
    console.print(Panel(tree, title="Graph Preview"))


def _validate(graph: Graph) -> None:
    errors = graph.validate()
    if errors:
        console.print("[red]Validation errors:[/red]")
        for e in errors:
            console.print(f"  [red]• {e}[/red]")
    else:
        console.print("[green]Graph is valid![/green]")


def _save(graph: Graph, storage: GoAStorage) -> None:
    errors = graph.validate()
    if errors:
        console.print("[red]Cannot save — fix validation errors first:[/red]")
        for e in errors:
            console.print(f"  [red]• {e}[/red]")
        return

    storage.save_graph(graph)
    console.print(f"[green]Saved graph '{graph.name}'[/green]")


def _show_nodes(graph: Graph) -> None:
    nodes_str = ", ".join(
        f"[cyan]{n}[/cyan]{'*' if n == graph.entry else ''}"
        for n in graph.nodes
    )
    console.print(f"  Nodes: {nodes_str}")

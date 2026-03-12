"""HTTP API server for GoA — lightweight, stdlib-only."""

from __future__ import annotations

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from goa.executor import GoAExecutor
from goa.models import Graph
from goa.storage import GoAStorage, _dict_to_graph, _graph_to_dict

STATIC_DIR = Path(__file__).parent / "static"


class GoAHandler(BaseHTTPRequestHandler):
    """Route requests to the appropriate GoA API handler."""

    storage: GoAStorage
    executor: GoAExecutor
    _bg_threads: dict[str, threading.Thread] = {}

    def log_message(self, format: str, *args) -> None:
        pass

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/goa":
            self._api_list_graphs()
        elif path.startswith("/api/goa/") and path.count("/") == 3:
            name = path.split("/")[3]
            self._api_get_graph(name)
        elif path.startswith("/api/goa/") and path.endswith("/run"):
            name = path.split("/")[3]
            self._api_run_status(name)
        elif path.startswith("/api/goa/") and path.endswith("/runs"):
            name = path.split("/")[3]
            self._api_list_runs(name)
        elif path == "" or path == "/":
            self._serve_static("index.html")
        else:
            fname = path.lstrip("/")
            self._serve_static(fname)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/goa":
            self._api_save_graph()
        elif path.startswith("/api/goa/") and path.endswith("/run"):
            name = path.split("/")[3]
            self._api_start_run(name)
        elif path.startswith("/api/goa/") and path.endswith("/stop"):
            name = path.split("/")[3]
            self._api_stop_run(name)
        else:
            self._json_response(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/goa/") and path.count("/") == 3:
            name = path.split("/")[3]
            self._api_delete_graph(name)
        else:
            self._json_response(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ------------------------------------------------------------------
    # API handlers
    # ------------------------------------------------------------------

    def _api_list_graphs(self) -> None:
        graphs = []
        for name in self.storage.list_graphs():
            g = self.storage.load_graph(name)
            graphs.append({
                "name": g.name,
                "description": g.description,
                "node_count": len(g.nodes),
                "entry": g.entry,
            })
        self._json_response(200, graphs)

    def _api_get_graph(self, name: str) -> None:
        try:
            g = self.storage.load_graph(name)
        except FileNotFoundError:
            self._json_response(404, {"error": f"graph '{name}' not found"})
            return
        data = _graph_to_dict(g)
        run_id = self.storage.latest_run_id(name)
        if run_id:
            run = self.storage.load_run(name, run_id)
            data["latest_run"] = {
                "run_id": run.run_id,
                "status": run.status,
                "started_at": run.started_at,
            }
        self._json_response(200, data)

    def _api_save_graph(self) -> None:
        body = self._read_body()
        try:
            data = json.loads(body)
            graph = _dict_to_graph(data)
        except (json.JSONDecodeError, KeyError) as e:
            self._json_response(400, {"error": str(e)})
            return
        errors = graph.validate()
        if errors:
            self._json_response(400, {"error": "validation failed", "details": errors})
            return
        self.storage.save_graph(graph)
        self._json_response(200, {"ok": True, "name": graph.name})

    def _api_delete_graph(self, name: str) -> None:
        self.storage.delete_graph(name)
        self._json_response(200, {"ok": True})

    def _api_start_run(self, name: str) -> None:
        try:
            graph = self.storage.load_graph(name)
        except FileNotFoundError:
            self._json_response(404, {"error": f"graph '{name}' not found"})
            return
        run_id = self.executor.start(graph, blocking=False)
        self._json_response(200, {"ok": True, "run_id": run_id})

    def _api_stop_run(self, name: str) -> None:
        run_id = self.storage.latest_run_id(name)
        if not run_id:
            self._json_response(404, {"error": "no active run"})
            return
        self.executor.stop(name, run_id)
        self._json_response(200, {"ok": True})

    def _api_run_status(self, name: str) -> None:
        run_id = self.storage.latest_run_id(name)
        if not run_id:
            self._json_response(200, {"status": "no_runs"})
            return
        run = self.storage.load_run(name, run_id)
        try:
            graph = self.storage.load_graph(name)
            node_names = list(graph.nodes.keys())
        except FileNotFoundError:
            node_names = list(run.node_states.keys())

        nodes = {}
        for n in node_names:
            s = self.storage.read_node_state(name, run_id, n)
            nodes[n] = {
                "status": s.status.value,
                "activated_by": s.activated_by,
                "output": s.output[:500],
                "error": s.error,
                "updated_at": s.updated_at,
            }

        self._json_response(200, {
            "run_id": run.run_id,
            "status": run.status,
            "started_at": run.started_at,
            "nodes": nodes,
        })

    def _api_list_runs(self, name: str) -> None:
        runs = []
        for rid in self.storage.list_runs(name):
            r = self.storage.load_run(name, rid)
            runs.append({
                "run_id": r.run_id,
                "status": r.status,
                "started_at": r.started_at,
            })
        self._json_response(200, runs)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_body(self) -> str:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode("utf-8")

    def _json_response(self, code: int, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _serve_static(self, filename: str) -> None:
        path = STATIC_DIR / filename
        if not path.exists() or not path.is_file():
            path = STATIC_DIR / "index.html"
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        content = path.read_bytes()
        content_type = "text/html"
        if filename.endswith(".js"):
            content_type = "application/javascript"
        elif filename.endswith(".css"):
            content_type = "text/css"
        elif filename.endswith(".json"):
            content_type = "application/json"
        elif filename.endswith(".svg"):
            content_type = "image/svg+xml"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def run_server(
    storage: GoAStorage,
    host: str = "127.0.0.1",
    port: int = 7862,
) -> None:
    """Start the GoA HTTP server."""
    GoAHandler.storage = storage
    GoAHandler.executor = GoAExecutor(storage)

    server = HTTPServer((host, port), GoAHandler)
    print(f"GoA server running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()

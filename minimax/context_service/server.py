#!/usr/bin/env python3
"""HTTP front-end for the SDD context module.

Endpoints
---------
GET  /health            service + seekdb status
GET  /                  endpoint index
GET  /state?task=...    PowerContext state slice for a task
GET  /mcp/tool          MCP-style tool definition
POST /context           {"task": "...", "per_kind": 3} -> context contract

Run:  python -m context_service.server --port 8765 --db-port 2896
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_module.docs_store import DocStore
from context_module.env import defaults as _db_defaults  # noqa: E402
from context_service.service import build_context, read_state  # noqa: E402


class ContextService:
    def __init__(
        self,
        *,
        store: DocStore,
        state_db: str,
        per_kind: int = 3,
        tool_path: Path | None = None,
    ) -> None:
        self.store = store
        self.state_db = state_db
        self.per_kind = per_kind
        self.tool_path = tool_path or (ROOT / "docs" / "mcp-tool.json")

    @property
    def label(self) -> str:
        return f"{self.store._connect_kwargs['host']}:{self.store._connect_kwargs['port']}/{self.store.database}"  # noqa: SLF001

    def document_count(self) -> int:
        return sum(len(self.store.list_kind(kind, limit=1000)) for kind in ("spec", "code", "test"))

    def health(self) -> dict:
        return {
            "ok": True,
            "service": "sdd-context-service",
            "seekdb": self.label,
            "documents": self.document_count(),
            "state_db": str(self.state_db),
            "retrieval": "bm25",
        }

    def tool_definition(self) -> dict:
        return json.loads(self.tool_path.read_text(encoding="utf-8"))


class Handler(BaseHTTPRequestHandler):
    service: ContextService

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/health", "/healthz"):
                self._send(200, self.service.health())
                return
            if parsed.path == "/":
                self._send(
                    200,
                    {
                        "service": "sdd-context-service",
                        "endpoints": ["GET /health", "GET /state?task=...", "GET /mcp/tool", "POST /context"],
                    },
                )
                return
            if parsed.path == "/state":
                task = (parse_qs(parsed.query).get("task") or [""])[0].strip()
                if not task:
                    self._send(400, {"error": "missing task query parameter"})
                    return
                self._send(
                    200,
                    read_state(task, store=self.service.store, state_db=self.service.state_db),
                )
                return
            if parsed.path == "/mcp/tool":
                self._send(200, self.service.tool_definition())
                return
            self._send(404, {"error": f"no route for {parsed.path}"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/context":
            self._send(404, {"error": f"no route for {parsed.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            task = str(payload.get("task", "")).strip()
            if not task:
                self._send(400, {"error": "body must contain a non-empty task"})
                return
            per_kind = int(payload.get("per_kind", self.service.per_kind))
            context = build_context(
                task,
                store=self.service.store,
                state_db=self.service.state_db,
                per_kind=max(1, min(per_kind, 10)),
            )
            self._send(200, context)
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[service] " + (fmt % args) + "\n")
        sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    _DB = _db_defaults()
    parser.add_argument("--db-host", default=str(_DB["host"]))
    parser.add_argument("--db-port", type=int, default=int(_DB["port"]))
    parser.add_argument("--db-user", default=str(_DB["user"]))
    parser.add_argument("--db-password", default=str(_DB["password"]))
    parser.add_argument("--database", default=str(_DB["database"]))
    parser.add_argument("--state-db", default=str(ROOT / ".sdd" / "state.db"))
    parser.add_argument("--per-kind", type=int, default=3)
    args = parser.parse_args(argv)

    store = DocStore(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=args.db_password,
        database=args.database,
    )
    service = ContextService(store=store, state_db=args.state_db, per_kind=args.per_kind)
    handler = type("BoundHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    host, port = server.server_address[:2]
    print(f"context service listening on http://{host}:{port} (seekdb {service.label})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

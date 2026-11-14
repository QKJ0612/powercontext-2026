#!/usr/bin/env python3
"""Start the HTTP context service, exercise every endpoint, and save evidence.

No external HTTP client is needed (urllib only).  The service talks to the real
seekdb on 127.0.0.1:2896 and the real PowerContext state database.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "service"
TASK = "给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变"
from context_module.env import defaults as _db_defaults

DB_PORT = int(_db_defaults()["port"])
STATE_DB = ROOT / ".sdd" / "state.db"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(method: str, url: str, payload: dict | None = None, *, timeout: float = 10.0) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def wait_health(base: str, log: list[str], *, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            status, payload = request("GET", f"{base}/health", timeout=2.0)
            if status == 200 and payload.get("ok"):
                return payload
            last_error = f"status={status} body={payload}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)
    raise RuntimeError(f"service did not become healthy: {last_error}")


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(ROOT))
    server_log = EVIDENCE / "server.log"
    with server_log.open("w", encoding="utf-8") as handle:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "context_service.server",
                "--port",
                str(port),
                "--db-port",
                str(DB_PORT),
                "--state-db",
                str(STATE_DB),
            ],
            cwd=ROOT,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    log.append(f"$ python -m context_service.server --port {port} --db-port {DB_PORT} (pid={server.pid})")
    checks: list[dict] = []
    try:
        health = wait_health(base, log)
        log.append(f"GET {base}/health -> {json.dumps(health, ensure_ascii=False)}")
        (EVIDENCE / "health.json").write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")
        checks.append({"name": "health ok", "ok": health.get("ok") is True and health.get("documents", 0) >= 3})

        status, context = request("POST", f"{base}/context", {"task": TASK})
        log.append(f"POST {base}/context -> HTTP {status}")
        (EVIDENCE / "context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
        retrieval = context.get("retrieval", {})
        checks.append(
            {
                "name": "POST /context returns specs/code/tests",
                "ok": status == 200
                and bool(context.get("specs"))
                and bool(context.get("code"))
                and bool(context.get("tests")),
                "detail": {
                    "specs": len(context.get("specs", [])),
                    "code": len(context.get("code", [])),
                    "tests": len(context.get("tests", [])),
                },
            }
        )
        checks.append(
            {
                "name": "retrieval is BM25 and sees spec version 2",
                "ok": retrieval.get("algorithm") == "bm25" and retrieval.get("spec_version", 0) >= 2,
                "detail": retrieval,
            }
        )
        checks.append(
            {
                "name": "spec comes from docs/spec-orders.md",
                "ok": any(item.get("source") == "docs/spec-orders.md" for item in context.get("specs", [])),
            }
        )

        status, state = request("GET", f"{base}/state?task={urllib.request.quote(TASK)}")
        log.append(f"GET {base}/state -> HTTP {status}")
        (EVIDENCE / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        checks.append(
            {
                "name": "GET /state returns the PowerContext slice",
                "ok": status == 200 and isinstance(state.get("state"), dict) and "completed" in state["state"],
            }
        )

        status, tool = request("GET", f"{base}/mcp/tool")
        log.append(f"GET {base}/mcp/tool -> HTTP {status}")
        (EVIDENCE / "mcp-tool.json").write_text(json.dumps(tool, ensure_ascii=False, indent=2), encoding="utf-8")
        checks.append({"name": "MCP tool descriptor served", "ok": status == 200 and tool.get("name") == "sdd_context"})
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        log.append(f"server stopped (exit={server.returncode})")

    ok = all(check["ok"] for check in checks)
    result = {
        "ok": ok,
        "base_url": base,
        "task": TASK,
        "checks": checks,
        "artifacts": {
            "context": str(EVIDENCE / "context.json"),
            "state": str(EVIDENCE / "state.json"),
            "health": str(EVIDENCE / "health.json"),
            "mcp_tool": str(EVIDENCE / "mcp-tool.json"),
        },
    }
    (EVIDENCE / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.append(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False))
    (EVIDENCE / "run.log").write_text("\n".join(log) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

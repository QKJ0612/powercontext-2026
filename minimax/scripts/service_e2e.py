#!/usr/bin/env python3
"""End-to-end: HTTP service supplies the context, minimax_patch.py consumes it.

Starts ``context_service.server`` on a free port, calls ``POST /context`` via
``minimax_patch.py --context-url``, replays the recorded MiniMax response, runs the
tests, then verifies that the session file records ``context_source=http`` and the
BM25 retrieval metadata.  No network call to MiniMax is made.
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
EVIDENCE = ROOT / "evidence" / "service" / "e2e"
TASK = "给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变"
from context_module.env import defaults as _db_defaults

DB_PORT = int(_db_defaults()["port"])


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_health(base: str, *, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("ok"):
                    return payload
                last = str(payload)
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)
    raise RuntimeError(f"service not healthy: {last}")


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log: list[str] = [f"$ python -m context_service.server --port {port} --db-port {DB_PORT}"]
    with (EVIDENCE / "server.log").open("w", encoding="utf-8") as handle:
        server = subprocess.Popen(
            [sys.executable, "-m", "context_service.server", "--port", str(port), "--db-port", str(DB_PORT)],
            cwd=ROOT,
            env=dict(os.environ),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    checks: list[dict] = []
    try:
        health = wait_health(base)
        log.append(f"health: {json.dumps(health, ensure_ascii=False)}")
        command = [
            sys.executable,
            "scripts/minimax_patch.py",
            "--task",
            TASK,
            "--port",
            str(DB_PORT),
            "--reset-before",
            "--offline",
            str(ROOT / "evidence" / "minimax_session.json"),
            "--context-url",
            f"{base}/context",
            "--evidence-dir",
            str(EVIDENCE / "patch"),
        ]
        log.append("$ " + " ".join(command))
        proc = subprocess.run(command, cwd=ROOT, env=dict(os.environ), capture_output=True, text=True)
        log.append(f"exit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        checks.append({"name": "minimax_patch.py --context-url exits 0", "ok": proc.returncode == 0})

        summary = json.loads((EVIDENCE / "patch" / "summary.json").read_text(encoding="utf-8"))
        session = json.loads((EVIDENCE / "patch" / "minimax_session.json").read_text(encoding="utf-8"))
        checks.append(
            {
                "name": "before failed / after all passed",
                "ok": summary["before_counts"]["failed"] == 3 and summary["after_counts"]["failed"] == 0,
                "detail": {"before": summary["before_counts"], "after": summary["after_counts"]},
            }
        )
        checks.append(
            {
                "name": "context came from the HTTP service",
                "ok": session.get("context_source") == "http"
                and session.get("context_url", "").startswith(base)
                and session.get("context", {}).get("retrieval", {}).get("algorithm") == "bm25",
                "detail": {
                    "context_source": session.get("context_source"),
                    "context_url": session.get("context_url"),
                    "retrieval": session.get("context", {}).get("retrieval"),
                },
            }
        )
        checks.append(
            {
                "name": "session keeps prompt + raw reply + diff provenance",
                "ok": bool(session.get("prompt")) and bool(session.get("raw_reply")) and session.get("mode") == "offline_replay",
                "detail": {"prompt_len": len(session.get("prompt") or ""), "mode": session.get("mode")},
            }
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        log.append(f"server stopped (exit={server.returncode})")

    ok = all(check["ok"] for check in checks)
    result = {"ok": ok, "base_url": base, "checks": checks}
    (EVIDENCE / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (EVIDENCE / "run.log").write_text("\n".join(log) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the MiniMax-assisted code change end to end and capture evidence.

Steps: build the task context, snapshot the before state, ask MiniMax for the patched
file, apply it, run the same tests before and after, and persist the task state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from context_module.cli import main as context_cli
from context_module.docs_store import DocStore
from context_module.selector import build_context
from context_module.state_store import record_task_state

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "sample_project"
EVIDENCE = ROOT / "evidence"
TARGET = Path("app/orders.py")
API_HOST = os.environ.get("MINIMAX_API_HOST", "https://api.minimaxi.com")
DEFAULT_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M3.1-Flash")
SYSTEM_PROMPT = (
    "You are a careful senior Python engineer. Apply exactly the requested change, keep the "
    "public API and existing behaviour intact, and return the complete updated file."
)
_FENCE = re.compile(r"```(?:python)?\s*(.*?)```", re.S)


def _api_key() -> str:
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if key:
        return key
    key_file = Path(os.environ.get("MINIMAX_KEY_FILE", Path.home() / ".minimax_key"))
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    raise SystemExit("MiniMax API key not found: set MINIMAX_API_KEY or ~/.minimax_key")


def _ask_minimax(task: str, context: dict, before: str, model: str) -> tuple[str, dict]:
    prompt = json.dumps(
        {
            "task": task,
            "specs": [{"source": item["source"], "excerpt": item["excerpt"]} for item in context["specs"]],
            "tests": [{"source": item["source"], "excerpt": item["excerpt"]} for item in context["tests"]],
            "file_to_change": str(TARGET),
            "current_file": before,
        },
        ensure_ascii=False,
        indent=2,
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    request = urllib.request.Request(
        f"{API_HOST}/v1/text/chatcompletion_v2",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.loads(response.read().decode("utf-8"))
    base = body.get("base_resp") or {}
    if base.get("status_code") not in (0, None):
        raise SystemExit(f"MiniMax API error: {base}")
    content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    blocks = _FENCE.findall(content)
    patched = blocks[-1].strip() if blocks else content.strip()
    if not patched.startswith(("from __future__", '"""', "#", "from ", "import ")):
        raise SystemExit("MiniMax did not return a plausible Python file")
    return patched, {"request_model": model, "usage": body.get("usage"), "raw_reply": content}


def _run_tests(label: str) -> tuple[int, str]:
    command = [sys.executable, "-m", "pytest", "-q", "tests"]
    completed = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True)
    output = completed.stdout + completed.stderr
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / f"test_{label}.log").write_text(f"$ {' '.join(command)}\nexit={completed.returncode}\n{output}", encoding="utf-8")
    return completed.returncode, output


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the MiniMax SDD demo")
    parser.add_argument("--task", default="给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    before_path = PROJECT / TARGET
    before = before_path.read_text(encoding="utf-8")
    (EVIDENCE / "before_orders.py").write_text(before, encoding="utf-8")

    store = DocStore()
    context = build_context(args.task, store=store, state_db=str(ROOT / ".sdd" / "state.db"))
    (EVIDENCE / "context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")

    before_rc, _ = _run_tests("before")
    patched, session = _ask_minimax(args.task, context, before, args.model)
    (EVIDENCE / "minimax_session.json").write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    before_path.write_text(patched + ("\n" if not patched.endswith("\n") else ""), encoding="utf-8")
    (EVIDENCE / "after_orders.py").write_text(before_path.read_text(encoding="utf-8"), encoding="utf-8")
    after_rc, _ = _run_tests("after")
    diff = subprocess.run(
        ["git", "diff", "--no-index", "--", str(EVIDENCE / "before_orders.py"), str(EVIDENCE / "after_orders.py")],
        capture_output=True,
        text=True,
    ).stdout
    (EVIDENCE / "diff.patch").write_text(diff, encoding="utf-8")

    record_task_state(
        ROOT / ".sdd" / "state.db",
        {
            "kind": "sdd-task",
            "task": args.task,
            "status": "completed" if after_rc == 0 and before_rc != 0 else "pending",
            "summary": args.task,
            "verification": {"before_exit": before_rc, "after_exit": after_rc},
            "context_sources": [item["source"] for item in context["specs"] + context["code"] + context["tests"]],
        },
    )
    context_cli(["--task", args.task, "--record-status", "completed", "--record-summary", args.task])
    print(json.dumps({"before_exit": before_rc, "after_exit": after_rc}, ensure_ascii=False))
    return 0 if after_rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

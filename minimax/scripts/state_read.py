#!/usr/bin/env python3
"""Read the persisted task state in a *fresh process* and optionally assert a summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_module.docs_store import DocStore  # noqa: E402
from context_module.selector import build_context  # noqa: E402

TASK = "给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=TASK)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2896)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--database", default="test")
    parser.add_argument("--state-db", default=str(ROOT / ".sdd" / "state.db"))
    parser.add_argument("--expect-summary", default="", help="summary that must be visible in state")
    parser.add_argument("--json-out", default="", help="write the full context JSON to this path")
    args = parser.parse_args(argv)

    store = DocStore(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
    )
    context = build_context(args.task, store=store, state_db=args.state_db)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")

    state = context["state"]
    summary = {
        "completed": state["completed"],
        "pending": state["pending"],
        "verified_count": len(state["verified"]),
        "history_count": len(state["history"]),
    }
    ok = True
    if args.expect_summary:
        seen = set(state["completed"]) | set(state["pending"])
        ok = args.expect_summary in seen
        summary["expected"] = args.expect_summary
        summary["expected_found"] = ok
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

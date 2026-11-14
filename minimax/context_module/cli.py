"""Command line entry point for the SDD context module."""

from __future__ import annotations

import argparse
import json
import sys

from context_module.docs_store import DocStore
from context_module.env import defaults as _db_defaults
from context_module.selector import build_context
from context_module.state_store import record_task_state


_DB = _db_defaults()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select SDD context for one development task.")
    parser.add_argument("--task", required=True, help="development task description")
    parser.add_argument("--state-db", default=".sdd/state.db", help="PowerContext SQLite database path")
    parser.add_argument("--host", default=str(_DB["host"]), help="seekdb host (SEEDB_HOST)")
    parser.add_argument("--port", type=int, default=int(_DB["port"]), help="seekdb port (SEEDB_PORT, default 2881)")
    parser.add_argument("--user", default=str(_DB["user"]), help="seekdb user (SEEDB_USER)")
    parser.add_argument("--password", default=str(_DB["password"]), help="seekdb password (SEEDB_PASSWORD)")
    parser.add_argument("--database", default=str(_DB["database"]), help="seekdb database (SEEDB_DATABASE)")
    parser.add_argument("--json", action="store_true", help="print the full JSON context")
    parser.add_argument("--record-status", choices=("pending", "completed"), help="persist a task state record")
    parser.add_argument("--record-summary", default="", help="summary stored with the task state record")
    parser.add_argument("--record-verification", default="", help="verification note stored with the record")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = DocStore(host=args.host, port=args.port, user=args.user, password=args.password, database=args.database)
    context = build_context(args.task, store=store, state_db=args.state_db)

    if args.record_status:
        record_task_state(
            args.state_db,
            {
                "kind": "sdd-task",
                "task": args.task,
                "status": args.record_status,
                "summary": args.record_summary or args.task,
                "verification": args.record_verification,
                "context_sources": [item["source"] for item in context["specs"] + context["code"] + context["tests"]],
            },
        )

    if args.json:
        json.dump(context, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"Task: {context['task']}")
    for label, key in (("Specs", "specs"), ("Code", "code"), ("Tests", "tests")):
        print(f"\n{label}:")
        for item in context[key]:
            print(f"  - [{item['score']}] {item['source']} :: {item['title']}")
    state = context["state"]
    print(f"\nCompleted: {state['completed']}")
    print(f"Pending: {state['pending']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

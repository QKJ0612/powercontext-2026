#!/usr/bin/env python3
"""Prove that PowerContext task state survives a module restart.

The demo (1) removes the state database so it starts empty, (2) records a task in one
Python process, (3) reads it back in a *different* Python process and checks that the
context module exposes it in ``state.pending`` / ``state.completed``.

Everything is written to ``evidence/state_reuse/`` so reviewers can inspect the exact
commands, PIDs, exit codes and JSON payloads.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT_FOR_IMPORT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(ROOT_FOR_IMPORT))

from context_module.env import defaults as _db_defaults

_DB = _db_defaults()

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "state_reuse"
TASK = "给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    paths = [str(ROOT)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _run(args: list[str], log: list[str]) -> tuple[int, str, str, int]:
    proc = subprocess.Popen(
        [sys.executable, *args],
        cwd=ROOT,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate()
    log.append(
        f"$ {' '.join(args)}\n# pid={proc.pid} exit={proc.returncode}\n"
        f"# stdout:\n{out}\n# stderr:\n{err}"
    )
    return proc.returncode, out, err, proc.pid


def _cli(port: int, state_db: str, task: str, extra: list[str]) -> list[str]:
    return [
        "-m",
        "context_module.cli",
        "--task",
        task,
        "--port",
        str(port),
        "--state-db",
        state_db,
        *extra,
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=TASK)
    parser.add_argument("--port", type=int, default=int(_DB["port"]))
    parser.add_argument("--state-db", default=".sdd/state.db")
    parser.add_argument("--evidence-dir", default=str(EVIDENCE))
    args = parser.parse_args(argv)

    evidence = Path(args.evidence_dir).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    state_db = (ROOT / args.state_db).resolve() if not Path(args.state_db).is_absolute() else Path(args.state_db)
    log: list[str] = [f"state reuse demo started (cwd={ROOT})", f"state db: {state_db}"]

    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(state_db) + suffix)
        if candidate.exists():
            candidate.unlink()
            log.append(f"removed {candidate}")
    state_db.parent.mkdir(parents=True, exist_ok=True)

    checks: list[dict] = []

    rc1, out1, err1, pid1 = _run(
        _cli(
            args.port,
            args.state_db,
            args.task,
            ["--record-status", "pending", "--record-summary", "state-reuse-demo pending"],
        ),
        log,
    )
    checks.append({"name": "record pending in process 1", "ok": rc1 == 0, "detail": {"pid": pid1, "exit": rc1}})

    rc2, out2, err2, pid2 = _run(_cli(args.port, args.state_db, args.task, ["--json"]), log)
    read1 = json.loads(out2) if out2.strip().startswith("{") else {}
    pending1 = read1.get("state", {}).get("pending", [])
    checks.append(
        {
            "name": "restart: a new process sees the pending record",
            "ok": rc2 == 0 and pid2 != pid1 and "state-reuse-demo pending" in pending1,
            "detail": {"pid": pid2, "pending": pending1},
        }
    )
    (evidence / "read_after_restart.json").write_text(
        json.dumps(read1, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rc3, out3, err3, pid3 = _run(
        _cli(
            args.port,
            args.state_db,
            args.task,
            ["--record-status", "completed", "--record-summary", "state-reuse-demo completed"],
        ),
        log,
    )
    checks.append({"name": "record completed in process 3", "ok": rc3 == 0, "detail": {"pid": pid3, "exit": rc3}})

    rc4, out4, err4, pid4 = _run(_cli(args.port, args.state_db, args.task, ["--json"]), log)
    read2 = json.loads(out4) if out4.strip().startswith("{") else {}
    completed2 = read2.get("state", {}).get("completed", [])
    checks.append(
        {
            "name": "restart: a new process sees the completed record",
            "ok": rc4 == 0 and pid4 != pid3 and "state-reuse-demo completed" in completed2,
            "detail": {"pid": pid4, "completed": completed2},
        }
    )
    (evidence / "read_after_second_restart.json").write_text(
        json.dumps(read2, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ok = all(check["ok"] for check in checks)
    result = {
        "ok": ok,
        "state_db": str(state_db),
        "checks": checks,
        "note": "Each line with 'powercontext' in stderr is emitted by context_module.state_store.",
    }
    (evidence / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.append(f"state reuse ok={ok}")
    (evidence / "run.log").write_text("\n".join(log) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

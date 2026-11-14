#!/usr/bin/env python3
"""Demonstrate spec-driven context: change the spec, re-import, context follows.

Sequence (all commands and exit codes are written to ``evidence/spec_update/run.log``):

1. import the current docs into seekdb and capture the *before* context;
2. edit ``docs/spec-orders.md`` (a visible marker in the title and in REQ-ORDER-3);
3. re-run ``init_seekdb.py`` and capture the *after* context;
4. assert the selected spec excerpt changed and contains the marker;
5. restore the original spec, re-import, and assert the excerpt is back to the baseline.

The repository is left clean; the before/after evidence stays in ``evidence/spec_update/``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_module.env import defaults as _db_defaults

_DB = _db_defaults()
DOC = ROOT / "docs" / "spec-orders.md"
EVIDENCE = ROOT / "evidence" / "spec_update"
TASK = "给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变"
MARKER = "SPEC-UPDATE-DEMO-2026-11-14"
OLD_TITLE = "# 订单查询规格（SDD 样例）"
NEW_TITLE = f"# 订单查询规格（SDD 样例｜{MARKER}）"
OLD_BOUNDARY = "- 边界值必须包含（等于下限的订单保留）。"
NEW_BOUNDARY = f"- 边界值必须包含（等于下限的订单保留；{MARKER} 已复核）。"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    paths = [str(ROOT)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _run(args: list[str], log: list[str]) -> tuple[int, str, str]:
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
    return proc.returncode, out, err


def _init(port: int, log: list[str]) -> int:
    rc, _, _ = _run(["scripts/init_seekdb.py", "--port", str(port)], log)
    return rc


def _context(port: int, path: Path, log: list[str]) -> dict:
    rc, out, err = _run(
        ["-m", "context_module.cli", "--task", TASK, "--port", str(port), "--json"], log
    )
    if rc != 0:
        raise SystemExit(f"context CLI failed: {err}")
    data = json.loads(out)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _spec_excerpt(context: dict) -> str:
    for item in context.get("specs", []):
        if item.get("source") == "docs/spec-orders.md":
            return item.get("excerpt", "")
    raise SystemExit("spec-orders.md was not selected in the context")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(_DB["port"]))
    parser.add_argument("--evidence-dir", default=str(EVIDENCE))
    args = parser.parse_args(argv)

    evidence = Path(args.evidence_dir).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    log: list[str] = [f"spec update demo started (cwd={ROOT})", f"spec file: {DOC}"]
    original = DOC.read_text(encoding="utf-8")
    checks: list[dict] = []
    try:
        if _init(args.port, log) != 0:
            raise SystemExit("baseline init_seekdb failed")
        before = _context(args.port, evidence / "context_before.json", log)
        before_excerpt = _spec_excerpt(before)

        updated = original.replace(OLD_TITLE, NEW_TITLE).replace(OLD_BOUNDARY, NEW_BOUNDARY)
        if updated == original:
            raise SystemExit("spec markers were not found; update the demo edit")
        DOC.write_text(updated, encoding="utf-8")
        (evidence / "spec_change.patch").write_text(
            f"--- a/minimax/docs/spec-orders.md\n+++ b/minimax/docs/spec-orders.md\n"
            f"-{OLD_TITLE}\n+{NEW_TITLE}\n-{OLD_BOUNDARY}\n+{NEW_BOUNDARY}\n",
            encoding="utf-8",
        )
        log.append(f"edited {DOC} with marker {MARKER}")

        if _init(args.port, log) != 0:
            raise SystemExit("re-import after the spec update failed")
        after = _context(args.port, evidence / "context_after.json", log)
        after_excerpt = _spec_excerpt(after)
        checks.append(
            {
                "name": "context excerpt changed after spec update + re-import",
                "ok": after_excerpt != before_excerpt and MARKER in after_excerpt,
                "detail": {"marker": MARKER, "after_contains_marker": MARKER in after_excerpt},
            }
        )
        (evidence / "excerpt_diff.txt").write_text(
            "before excerpt:\n" + before_excerpt + "\n\nafter excerpt:\n" + after_excerpt + "\n",
            encoding="utf-8",
        )
        log.append(f"before excerpt: {before_excerpt}")
        log.append(f"after excerpt: {after_excerpt}")
    finally:
        DOC.write_text(original, encoding="utf-8")
        log.append(f"restored {DOC}")
        if _init(args.port, log) != 0:
            log.append("WARNING: re-import after restore failed")

    restored = _context(args.port, evidence / "context_restored.json", log)
    restored_excerpt = _spec_excerpt(restored)
    checks.append(
        {
            "name": "spec restored and re-imported (repository left clean)",
            "ok": restored_excerpt == before_excerpt and MARKER not in restored_excerpt,
            "detail": {"marker_absent": MARKER not in restored_excerpt},
        }
    )
    ok = all(check["ok"] for check in checks)
    result = {"ok": ok, "marker": MARKER, "spec": str(DOC), "checks": checks}
    (evidence / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.append(f"spec update demo ok={ok}")
    (evidence / "run.log").write_text("\n".join(log) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

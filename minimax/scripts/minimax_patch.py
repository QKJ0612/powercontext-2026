"""Run the MiniMax-assisted code change end to end and capture evidence.

The default path builds the task context, snapshots the before state, asks MiniMax for
the patched file, applies it, runs the same tests before and after, and persists the
task state.

Two resilience modes are available for review environments without MiniMax quota or
network access:

* ``--offline`` replays the model reply recorded in ``evidence/minimax_session.json``
  and labels the run as an offline replay (it never claims to be a live call).
* ``--selfcheck`` verifies the recorded before/after round trip without an API key and
  without any network access.

Live calls walk a model fallback chain (``MINIMAX_MODEL`` -> M3.1-Flash -> M2.5) and an
endpoint fallback chain (``api.minimaxi.com`` -> ``api.minimax.chat``), retry transient
failures with exponential backoff, and record every attempt in
``evidence/model_attempts.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT_FOR_IMPORT = Path(__file__).resolve().parents[1]
if str(ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(ROOT_FOR_IMPORT))

from context_module.docs_store import DocStore
from context_module.env import defaults as _db_defaults
from context_module.selector import build_context
from context_module.state_store import record_task_state

ROOT = ROOT_FOR_IMPORT
_DB = _db_defaults()
PROJECT = ROOT / "sample_project"
EVIDENCE = ROOT / "evidence"
TARGET = Path("app/orders.py")
DEFAULT_MODELS = ("MiniMax-M3.1-Flash", "MiniMax-M2.5")
DEFAULT_ENDPOINTS = ("https://api.minimaxi.com", "https://api.minimax.chat")
DEFAULT_SESSION = EVIDENCE / "minimax_session.json"
DEFAULT_BEFORE_SNAPSHOT = EVIDENCE / "before_orders.py"
SYSTEM_PROMPT = (
    "You are a careful senior Python engineer. Apply exactly the requested change, keep the "
    "public API and existing behaviour intact, and return the complete updated file. "
    "Return only one Python code block with the whole file."
)
_FENCE = re.compile(r"```(?:python)?\s*(.*?)```", re.S)

# The selfcheck is pinned to the first task's tests so that adding a second task later
# (new test module) keeps this historical round trip verifiable.
SELFCHECK_TESTS = ("tests/test_orders.py",)
SELFCHECK_TARGET_FAILURES = (
    "test_combines_status_and_minimum_amount",
    "test_filters_by_minimum_amount",
    "test_minimum_amount_is_inclusive_boundary",
)


class ModelError(RuntimeError):
    """A failed model call; ``retryable`` drives the bounded retry loop."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ModelReplyError(RuntimeError):
    """A model reply that cannot be used as a Python source file."""


class ModelChainExhausted(RuntimeError):
    """Every model/endpoint pair failed; carries the recorded attempts."""

    def __init__(self, attempts: list[dict], message: str) -> None:
        super().__init__(message)
        self.attempts = attempts


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = item.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _models(args: argparse.Namespace) -> list[str]:
    chain: list[str] = []
    env_model = os.environ.get("MINIMAX_MODEL", "").strip()
    if env_model:
        chain.append(env_model)
    chain.extend(args.model or [])
    chain.extend(DEFAULT_MODELS)
    return _dedupe(chain)


def _endpoints(args: argparse.Namespace) -> list[str]:
    chain: list[str] = []
    env_host = os.environ.get("MINIMAX_API_HOST", "").strip()
    if env_host:
        chain.append(env_host)
    chain.extend(args.endpoint or [])
    chain.extend(DEFAULT_ENDPOINTS)
    return _dedupe([item.rstrip("/") for item in chain])


def _api_key() -> str:
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if key:
        return key
    key_file = Path(os.environ.get("MINIMAX_KEY_FILE", Path.home() / ".minimax_key"))
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    raise SystemExit("MiniMax API key not found: set MINIMAX_API_KEY or ~/.minimax_key")


def _prompt(task: str, context: dict, before: str) -> str:
    return json.dumps(
        {
            "task": task,
            "specs": [{"source": item["source"], "excerpt": item["excerpt"]} for item in context["specs"]],
            "tests": [{"source": item["source"], "excerpt": item["excerpt"]} for item in context["tests"]],
            "file_to_change": str(TARGET),
            "current_file": before,
            "requirements": [
                "Apply exactly the requested change and nothing else.",
                "Keep existing keyword arguments, return order and behaviour unchanged.",
                "Return the complete file in one fenced code block.",
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def _extract_python(content: str) -> str:
    if not content.strip():
        raise ModelReplyError("model returned an empty reply")
    blocks = _FENCE.findall(content)
    patched = blocks[-1].strip() if blocks else content.strip()
    try:
        compile(patched, str(TARGET), "exec")
    except SyntaxError as exc:
        raise ModelReplyError(f"model reply is not valid Python: {exc}") from exc
    if "def find_orders" not in patched:
        raise ModelReplyError("model reply does not define find_orders")
    return patched if patched.endswith("\n") else patched + "\n"


def _post_once(url: str, payload: dict, key: str, timeout: float) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        retryable = exc.code in (408, 409, 429, 500, 502, 503, 504)
        raise ModelError(f"HTTP {exc.code}: {detail}", retryable=retryable) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise ModelError(f"network error: {exc}", retryable=True) from exc


def _validate_body(body: dict) -> None:
    if isinstance(body.get("error"), dict):
        raise ModelError(f"MiniMax API error: {body['error']}")
    base = body.get("base_resp") or {}
    if base.get("status_code") not in (0, None):
        raise ModelError(f"MiniMax API error: {base}", retryable=base.get("status_code") in (1000, 1002))


def _call_with_retries(
    attempts: list[dict],
    *,
    endpoint: str,
    model: str,
    payload: dict,
    key: str,
    timeout: float,
    retries: int,
    backoff: float,
) -> dict:
    last_error: ModelError | None = None
    for number in range(1, retries + 1):
        started = time.monotonic()
        try:
            status, body = _post_once(f"{endpoint}/v1/chat/completions", payload, key, timeout)
            _validate_body(body)
        except ModelError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            attempts.append(
                {
                    "endpoint": endpoint,
                    "model": model,
                    "attempt": number,
                    "ok": False,
                    "retryable": exc.retryable,
                    "elapsed_ms": elapsed_ms,
                    "error": str(exc),
                    "at": _now(),
                }
            )
            print(f"[attempt {number}/{retries}] {model} @ {endpoint}: {exc}")
            last_error = exc
            if not exc.retryable or number >= retries:
                break
            time.sleep(backoff * (2 ** (number - 1)))
            continue
        elapsed_ms = int((time.monotonic() - started) * 1000)
        attempts.append(
            {
                "endpoint": endpoint,
                "model": model,
                "attempt": number,
                "ok": True,
                "http_status": status,
                "request_id": body.get("id"),
                "elapsed_ms": elapsed_ms,
                "at": _now(),
            }
        )
        print(f"[attempt {number}/{retries}] {model} @ {endpoint}: ok in {elapsed_ms} ms")
        return body
    raise last_error or ModelError("unknown model failure")


def ask_online(
    task: str,
    context: dict,
    before: str,
    *,
    models: list[str],
    endpoints: list[str],
    key: str,
    timeout: float,
    retries: int,
    backoff: float,
) -> tuple[str, dict, list[dict]]:
    prompt = _prompt(task, context, before)
    attempts: list[dict] = []
    started = time.monotonic()
    for endpoint in endpoints:
        for model in models:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 4096,
            }
            print(f"[online] trying model={model} endpoint={endpoint}")
            try:
                body = _call_with_retries(
                    attempts,
                    endpoint=endpoint,
                    model=model,
                    payload=payload,
                    key=key,
                    timeout=timeout,
                    retries=retries,
                    backoff=backoff,
                )
                content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                patched = _extract_python(content)
            except (ModelError, ModelReplyError) as exc:
                print(f"[fallback] {model} @ {endpoint} unusable: {exc}")
                if isinstance(exc, ModelReplyError):
                    attempts.append(
                        {
                            "endpoint": endpoint,
                            "model": model,
                            "attempt": 1,
                            "ok": False,
                            "retryable": False,
                            "elapsed_ms": 0,
                            "error": f"unusable reply: {exc}",
                            "at": _now(),
                        }
                    )
                continue
            elapsed_ms = int((time.monotonic() - started) * 1000)
            session = {
                "mode": "online",
                "recorded_at": _now(),
                "task": task,
                "request_model": model,
                "endpoint": endpoint,
                "request_id": body.get("id"),
                "elapsed_ms": elapsed_ms,
                "usage": body.get("usage"),
                "prompt": prompt,
                "target_sha256_before": _sha256(before),
                "patched_sha256": _sha256(patched),
                "attempts": attempts,
                "raw_reply": content,
            }
            print(f"[online] accepted model={model} endpoint={endpoint} in {elapsed_ms} ms")
            return patched, session, attempts
    raise ModelChainExhausted(attempts, "all model/endpoint combinations failed")


def replay_offline(session_path: Path, before: str) -> tuple[str, dict]:
    if not session_path.exists():
        raise SystemExit(
            f"recorded session not found: {session_path}; run one live call first "
            "(python scripts/minimax_patch.py --port 2896)"
        )
    data = json.loads(session_path.read_text(encoding="utf-8"))
    raw_reply = data.get("raw_reply") or ""
    expected = data.get("target_sha256_before")
    if expected and _sha256(before) != expected:
        raise SystemExit(
            "recorded reply was produced for a different input file "
            f"(expected sha256 {expected[:12]}..., current {_sha256(before)[:12]}...); "
            "use --reset-before to restore the recorded pre-change snapshot"
        )
    patched = _extract_python(raw_reply)
    message = (
        "offline replay of recorded MiniMax response from "
        f"{session_path} (recorded model={data.get('request_model')}, "
        f"recorded_at={data.get('recorded_at')}); no network call was made"
    )
    print(f"[offline] {message}")
    session = {
        "mode": "offline_replay",
        "replayed_at": _now(),
        "source_session": str(session_path),
        "recorded_at": data.get("recorded_at"),
        "request_model": data.get("request_model"),
        "recorded_endpoint": data.get("endpoint"),
        "recorded_elapsed_ms": data.get("elapsed_ms"),
        "target_sha256_before": _sha256(before),
        "patched_sha256": _sha256(patched),
        "note": message,
        "raw_reply": raw_reply,
        "task": data.get("task"),
        "system_prompt": data.get("system_prompt"),
        "prompt": data.get("prompt"),
        "usage": data.get("usage"),
        "context": data.get("context"),
    }
    return patched, session


def _run_pytest(
    project: Path,
    targets: list[str],
    log_path: Path,
    *,
    header: list[str] | None = None,
) -> tuple[int, str]:
    command = [sys.executable, "-m", "pytest", "-q", *targets]
    completed = subprocess.run(command, cwd=project, capture_output=True, text=True)
    output = completed.stdout + completed.stderr
    lines = [f"# {line}" for line in (header or [])]
    lines.extend([f"$ {' '.join(command)}", f"# cwd: {project}", f"exit={completed.returncode}"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n" + output, encoding="utf-8")
    return completed.returncode, output


def _counts(output: str) -> dict[str, int]:
    failed = re.search(r"(\d+) failed", output)
    passed = re.search(r"(\d+) passed", output)
    errors = re.search(r"(\d+) error", output)
    return {
        "failed": int(failed.group(1)) if failed else 0,
        "passed": int(passed.group(1)) if passed else 0,
        "errors": int(errors.group(1)) if errors else 0,
    }


def _failed_names(output: str) -> list[str]:
    return sorted(re.findall(r"^FAILED\s+([^\s:]+::[^\s]+)", output, re.M))


def _write_diff(evidence: Path) -> None:
    completed = subprocess.run(
        [
            "git",
            "diff",
            "--no-index",
            "--",
            str(evidence / "before_orders.py"),
            str(evidence / "after_orders.py"),
        ],
        capture_output=True,
        text=True,
    )
    (evidence / "diff.patch").write_text(completed.stdout, encoding="utf-8")


def _load_or_build_context(args: argparse.Namespace, evidence: Path) -> tuple[dict, str]:
    if args.context_url:
        body = json.dumps({"task": args.task}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            args.context_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            context = json.loads(response.read().decode("utf-8"))
        print(f"[service] context fetched from {args.context_url}")
        return context, "http"
    try:
        store = DocStore(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            database=args.database,
        )
        context = build_context(args.task, store=store, state_db=str(ROOT / ".sdd" / "state.db"))
        return context, "seekdb"
    except Exception as exc:  # pragma: no cover - depends on the local seekdb service
        recorded = evidence / "context.json"
        if args.offline and recorded.exists():
            print(f"[offline] seekdb unavailable ({exc}); using recorded context {recorded}")
            return json.loads(recorded.read_text(encoding="utf-8")), "recorded"
        raise


def _write_run_log(evidence: Path, lines: list[str]) -> None:
    (evidence / "run.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_attempts(
    evidence: Path,
    models: list[str],
    endpoints: list[str],
    attempts: list[dict],
    *,
    winner: dict | None,
    elapsed_ms: int | None = None,
) -> None:
    payload = {
        "generated_at": _now(),
        "models": models,
        "endpoints": endpoints,
        "attempts": attempts,
        "winner": winner,
        "failed": winner is None,
    }
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    (evidence / "model_attempts.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _resolve_evidence(args: argparse.Namespace) -> Path:
    path = Path(args.evidence_dir)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def run_selfcheck(args: argparse.Namespace) -> int:
    """Verify the recorded first-task round trip offline: 3 fail before, 6 pass after."""

    evidence = _resolve_evidence(args)
    evidence.mkdir(parents=True, exist_ok=True)
    session_path = Path(args.offline).resolve() if args.offline else DEFAULT_SESSION
    snapshot = Path(args.before_snapshot).resolve()
    if not snapshot.exists():
        raise SystemExit(f"before snapshot not found: {snapshot}")
    if not session_path.exists():
        raise SystemExit(
            f"recorded session not found: {session_path}; run one live call first so the "
            "offline replay has a recorded MiniMax response to replay"
        )

    lines: list[str] = [
        f"selfcheck started at {_now()}",
        "mode: offline (no model network call, no API key required)",
        f"before snapshot: {snapshot}",
        f"recorded session: {session_path}",
    ]
    checks: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="minimax-selfcheck-") as tmp:
        project = Path(tmp) / "sample_project"
        shutil.copytree(PROJECT, project, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        target = project / TARGET
        before_text = snapshot.read_text(encoding="utf-8")
        target.write_text(before_text, encoding="utf-8")
        lines.append(f"temporary project: {project} (working tree untouched)")

        before_rc, before_out = _run_pytest(
            project,
            list(SELFCHECK_TESTS),
            evidence / "test_before_offline.log",
            header=[
                "selfcheck: temporary copy of sample_project",
                "app/orders.py restored from the recorded pre-change snapshot",
                "offline mode: no network access",
            ],
        )
        before_counts = _counts(before_out)
        before_failed = _failed_names(before_out)
        checks.append(
            {
                "name": "before: 3 target tests fail",
                "ok": before_rc != 0
                and before_counts["failed"] == 3
                and before_counts["passed"] == 3
                and before_failed
                == sorted(f"tests/test_orders.py::{name}" for name in SELFCHECK_TARGET_FAILURES),
                "detail": before_counts,
            }
        )
        lines.append(f"before: exit={before_rc} counts={before_counts} failed={before_failed}")

        patched, session = replay_offline(session_path, before_text)
        target.write_text(patched, encoding="utf-8")
        (evidence / "selfcheck_session.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        after_rc, after_out = _run_pytest(
            project,
            list(SELFCHECK_TESTS),
            evidence / "test_after_offline.log",
            header=[
                "selfcheck: temporary copy of sample_project",
                "app/orders.py replaced by an offline replay of the recorded MiniMax response",
                "offline mode: no network access",
            ],
        )
        after_counts = _counts(after_out)
        checks.append(
            {
                "name": "after: all 6 tests pass",
                "ok": after_rc == 0 and after_counts["passed"] == 6 and after_counts["failed"] == 0,
                "detail": after_counts,
            }
        )
        lines.append(f"after: exit={after_rc} counts={after_counts}")

    checks.append(
        {
            "name": "replay is labelled offline",
            "ok": session["mode"] == "offline_replay" and "no network call was made" in session["note"],
            "detail": session["mode"],
        }
    )
    ok = all(check["ok"] for check in checks)
    result = {
        "generated_at": _now(),
        "mode": "offline_selfcheck",
        "ok": ok,
        "session": str(session_path),
        "before_snapshot": str(snapshot),
        "checks": checks,
        "logs": {
            "before": str(evidence / "test_before_offline.log"),
            "after": str(evidence / "test_after_offline.log"),
        },
    }
    (evidence / "selfcheck.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines.append(f"selfcheck ok={ok}")
    _write_run_log(evidence, lines)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the MiniMax SDD demo")
    parser.add_argument("--task", default="给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变")
    parser.add_argument("--model", action="append", help="extra model to try before the built-in chain")
    parser.add_argument("--endpoint", action="append", help="extra endpoint to try before the built-in chain")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=2, help="attempts per model/endpoint pair")
    parser.add_argument("--backoff", type=float, default=1.0, help="base backoff seconds between retries")
    parser.add_argument("--offline", nargs="?", const=str(DEFAULT_SESSION), help="replay a recorded session; no network")
    parser.add_argument(
        "--context-url",
        default="",
        help="fetch the context contract from a running service, e.g. http://127.0.0.1:8765/context",
    )
    parser.add_argument("--selfcheck", action="store_true", help="offline before/after selfcheck, no API key")
    parser.add_argument("--reset-before", action="store_true", help="restore the recorded pre-change snapshot first")
    parser.add_argument("--before-snapshot", default=str(DEFAULT_BEFORE_SNAPSHOT), help="snapshot used by --reset-before")
    parser.add_argument("--evidence-dir", default=str(EVIDENCE), help="where to write evidence files")
    parser.add_argument("--host", default=str(_DB["host"]), help="seekdb host (SEEDB_HOST)")
    parser.add_argument("--port", type=int, default=int(_DB["port"]), help="seekdb port (SEEDB_PORT, default 2881)")
    parser.add_argument("--user", default=str(_DB["user"]), help="seekdb user (SEEDB_USER)")
    parser.add_argument("--password", default=str(_DB["password"]), help="seekdb password (SEEDB_PASSWORD)")
    parser.add_argument("--database", default=str(_DB["database"]), help="seekdb database (SEEDB_DATABASE)")
    args = parser.parse_args(argv)

    if args.selfcheck:
        return run_selfcheck(args)

    evidence = _resolve_evidence(args)
    evidence.mkdir(parents=True, exist_ok=True)
    lines = [f"run started at {_now()}", f"mode: {'offline' if args.offline else 'online'}", f"task: {args.task}"]

    target_path = PROJECT / TARGET
    if args.reset_before:
        snapshot = Path(args.before_snapshot).resolve()
        if not snapshot.exists():
            raise SystemExit(f"before snapshot not found: {snapshot}")
        target_path.write_text(snapshot.read_text(encoding="utf-8"), encoding="utf-8")
        lines.append(f"restored pre-change snapshot: {snapshot}")

    before = target_path.read_text(encoding="utf-8")
    (evidence / "before_orders.py").write_text(before, encoding="utf-8")
    lines.append(f"before sha256: {_sha256(before)}")

    context, context_source = _load_or_build_context(args, evidence)
    (evidence / "context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    lines.append(f"context source: {context_source}")

    before_rc, before_out = _run_pytest(
        PROJECT,
        ["tests"],
        evidence / "test_before.log",
        header=[f"task: {args.task}", f"mode: {'offline' if args.offline else 'online'}"],
    )
    before_counts = _counts(before_out)
    lines.append(f"before tests: exit={before_rc} counts={before_counts}")

    session: dict
    if args.offline:
        session_path = Path(args.offline).resolve()
        patched, session = replay_offline(session_path, before)
        after_label = "test_after_offline.log"
    else:
        models = _models(args)
        endpoints = _endpoints(args)
        lines.append(f"model chain: {models}")
        lines.append(f"endpoint chain: {endpoints}")
        try:
            patched, session, attempts = ask_online(
                args.task,
                context,
                before,
                models=models,
                endpoints=endpoints,
                key=_api_key(),
                timeout=args.timeout,
                retries=args.retries,
                backoff=args.backoff,
            )
        except ModelChainExhausted as exc:
            _write_attempts(evidence, models, endpoints, exc.attempts, winner=None)
            lines.append(f"online call failed: {exc}")
            lines.append(f"attempts recorded: {evidence / 'model_attempts.json'}")
            _write_run_log(evidence, lines)
            raise SystemExit(
                f"{exc}; every attempt is recorded in model_attempts.json"
            ) from exc
        _write_attempts(
            evidence,
            models,
            endpoints,
            attempts,
            winner={"model": session["request_model"], "endpoint": session["endpoint"]},
            elapsed_ms=session["elapsed_ms"],
        )
        after_label = "test_after.log"

    session["context_source"] = context_source
    session["context_url"] = args.context_url or None
    session["context"] = context
    (evidence / "minimax_session.json").write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    target_path.write_text(patched, encoding="utf-8")
    (evidence / "after_orders.py").write_text(patched, encoding="utf-8")
    after_rc, after_out = _run_pytest(
        PROJECT,
        ["tests"],
        evidence / after_label,
        header=[f"task: {args.task}", f"mode: {session['mode']}"],
    )
    after_counts = _counts(after_out)
    lines.append(f"after tests: exit={after_rc} counts={after_counts}")
    if session["mode"] == "offline_replay":
        lines.append("offline replay of recorded MiniMax response; no network call was made")

    _write_diff(evidence)
    record_task_state(
        ROOT / ".sdd" / "state.db",
        {
            "kind": "sdd-task",
            "task": args.task,
            "status": "completed" if after_rc == 0 and before_rc != 0 else "pending",
            "summary": args.task,
            "verification": {
                "before_exit": before_rc,
                "after_exit": after_rc,
                "before_counts": before_counts,
                "after_counts": after_counts,
                "mode": session["mode"],
                "model": session.get("request_model"),
                "endpoint": session.get("endpoint") or session.get("recorded_endpoint"),
            },
            "context_sources": [item["source"] for item in context["specs"] + context["code"] + context["tests"]],
        },
    )

    summary = {
        "mode": session["mode"],
        "model": session.get("request_model"),
        "endpoint": session.get("endpoint") or session.get("recorded_endpoint"),
        "elapsed_ms": session.get("elapsed_ms"),
        "before_exit": before_rc,
        "before_counts": before_counts,
        "after_exit": after_rc,
        "after_counts": after_counts,
        "evidence_dir": str(evidence),
    }
    (evidence / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines.append(f"summary: {json.dumps(summary, ensure_ascii=False)}")
    _write_run_log(evidence, lines)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if after_rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

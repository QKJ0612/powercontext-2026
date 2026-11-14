#!/usr/bin/env bash
# One-command reproduction of the MiniMax + seekdb + PowerContext loop.
#
#   bash minimax/run_demo.sh              # offline replay (no API key needed)
#   bash minimax/run_demo.sh --online     # real MiniMax call (needs a working key/quota)
#   bash minimax/run_demo.sh --reset      # restore the "before" snapshot and clear state
#
# Steps: reset -> init seekdb -> state reuse demo -> context -> patch/tests -> persist state
#        -> fresh process reads the state back.
set -euo pipefail

MINIMAX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$MINIMAX/.." && pwd)"
HOST="${SEEDB_HOST:-${SEEKDB_HOST:-127.0.0.1}}"
PORT="${SEEDB_PORT:-${SEEKDB_PORT:-2881}}"
DB_USER="${SEEDB_USER:-${SEEKDB_USER:-root}}"
DB_PASSWORD="${SEEDB_PASSWORD:-${SEEKDB_PASSWORD:-}}"
DATABASE="${SEEDB_DATABASE:-${SEEKDB_DATABASE:-test}}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
TASK="给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变"
ACTION="${1:-run}"
EVID="$MINIMAX/evidence/demo"
LOG="$EVID/run.log"
BEFORE="$MINIMAX/evidence/before_orders.py"
SESSION="$MINIMAX/evidence/minimax_session.json"

if [ -n "${POWERCONTEXT_SRC:-}" ]; then
  :
else
  for candidate in "$ROOT/../powercontext-2026/src" "$HOME/powercontext-2026/src" "$ROOT/powercontext-2026/src"; do
    if [ -d "$candidate" ]; then POWERCONTEXT_SRC="$candidate"; break; fi
  done
fi
if [ -n "${POWERCONTEXT_SRC:-}" ]; then
  export PYTHONPATH="$MINIMAX:$POWERCONTEXT_SRC${PYTHONPATH:+:$PYTHONPATH}"
else
  export PYTHONPATH="$MINIMAX${PYTHONPATH:+:$PYTHONPATH}"
fi

log() { printf '%s\n' "$*" | tee -a "$LOG"; }

step() {
  local name="$1"; shift
  log "=== $name ==="
  local out="$EVID/$name.log"
  set +e
  "$@" >"$out" 2>&1
  local rc=$?
  set -e
  cat "$out" >>"$LOG"
  log "exit=$rc (log: $out)"
  if [ "$rc" -ne 0 ]; then log "FAILED: $name"; exit "$rc"; fi
}

capture() {
  local name="$1"; local target="$2"; shift 2
  log "=== $name ==="
  local err="$EVID/$name.err"
  set +e
  "$@" >"$target" 2>"$err"
  local rc=$?
  set -e
  cat "$err" >>"$LOG"
  log "exit=$rc (json: $target)"
  if [ "$rc" -ne 0 ]; then log "FAILED: $name"; cat "$err"; exit "$rc"; fi
}

reset_state() {
  cp "$BEFORE" "$MINIMAX/sample_project/app/orders.py"
  rm -rf "$MINIMAX/.sdd"
  "$PYTHON_BIN" "$MINIMAX/scripts/init_seekdb.py" \
    --host "$HOST" --port "$PORT" --user "$DB_USER" --password "$DB_PASSWORD" --database "$DATABASE"
}

if [ "$ACTION" = "--reset" ] || [ "$ACTION" = "reset" ]; then
  mkdir -p "$EVID"
  : >"$LOG"
  log "reset: restoring sample_project/app/orders.py from $BEFORE"
  reset_state | tee -a "$LOG"
  log "reset complete: state db removed, docs re-imported into $HOST:$PORT/$DATABASE"
  exit 0
fi

MODE="offline"
if [ "$ACTION" = "--online" ] || [ "$ACTION" = "online" ]; then
  MODE="online"
fi

mkdir -p "$EVID"
: >"$LOG"
log "run_demo mode=$MODE host=$HOST port=$PORT database=$DATABASE"
log "python: $("$PYTHON_BIN" --version 2>&1)  PYTHONPATH=$PYTHONPATH"

MISSING="$("$PYTHON_BIN" - <<'PY'
import importlib.util
missing = [name for name in ("powercontext", "pymysql", "pytest") if importlib.util.find_spec(name) is None]
print(" ".join(missing))
PY
)"
if [ -n "$MISSING" ]; then
  log "ERROR: missing Python package(s): $MISSING"
  log "Install them with:"
  log "  $PYTHON_BIN -m pip install -r \"$MINIMAX/requirements.txt\""
  log "If you run PowerContext from the competition source tree instead, set POWERCONTEXT_SRC=<repo>/src."
  exit 2
fi
if ! "$PYTHON_BIN" - "$HOST" "$PORT" "$DB_USER" "$DB_PASSWORD" <<'PY'
import sys
import pymysql

host, port, user, password = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
try:
    pymysql.connect(host=host, port=port, user=user, password=password, connect_timeout=3).close()
except Exception as exc:  # noqa: BLE001
    print(f"seekdb not reachable at {host}:{port} as user={user}: {type(exc).__name__}: {exc}")
    print("common causes: seekdb is not running; wrong port (stock seekdb uses 2881, this lab used 2896); wrong user/password")
    print("start a local instance or point SEEDB_HOST/SEEDB_PORT/SEEDB_USER/SEEDB_PASSWORD at yours; see README")
    raise SystemExit(1)
PY
then
  log "ERROR: seekdb is not reachable on $HOST:$PORT (database=$DATABASE)."
  log "Diagnostics: try 'mysql -h $HOST -P $PORT -u $DB_USER -e "select 1"', or start seekdb and set SEEDB_PORT."
  exit 2
fi

# 1) Start from the documented initial state: before snapshot + empty task state.
step restore_before "$PYTHON_BIN" -c "import shutil; shutil.copyfile(r'$BEFORE', r'$MINIMAX/sample_project/app/orders.py')"
rm -rf "$MINIMAX/.sdd"
step init_seekdb "$PYTHON_BIN" "$MINIMAX/scripts/init_seekdb.py" \
  --host "$HOST" --port "$PORT" --user "$DB_USER" --password "$DB_PASSWORD" --database "$DATABASE"

# 2) Prove PowerContext state write + restart read from an empty state db.
step state_reuse "$PYTHON_BIN" "$MINIMAX/scripts/state_reuse_demo.py" \
  --port "$PORT" --state-db "$MINIMAX/.sdd/state.db"

# 3) Context selection for the task (this is what is sent to the model).
capture context_before "$EVID/context_cli.json" "$PYTHON_BIN" -m context_module.cli \
  --task "$TASK" --host "$HOST" --port "$PORT" --user "$DB_USER" --password "$DB_PASSWORD" \
  --database "$DATABASE" --state-db "$MINIMAX/.sdd/state.db" --json

# 4) Patch: offline replay by default, real call only with --online.
if [ "$MODE" = "online" ]; then
  step minimax_online "$PYTHON_BIN" "$MINIMAX/scripts/minimax_patch.py" \
    --task "$TASK" --host "$HOST" --port "$PORT" --user "$DB_USER" --password "$DB_PASSWORD" \
    --database "$DATABASE" \
    --reset-before --evidence-dir "$EVID/patch"
else
  step minimax_offline "$PYTHON_BIN" "$MINIMAX/scripts/minimax_patch.py" \
    --task "$TASK" --host "$HOST" --port "$PORT" --user "$DB_USER" --password "$DB_PASSWORD" \
    --database "$DATABASE" \
    --reset-before --offline "$SESSION" --evidence-dir "$EVID/patch"
fi

# 5) Fresh process (the "restarted module") reads the persisted state back.
capture state_after_restart "$EVID/state_after_restart.json" "$PYTHON_BIN" "$MINIMAX/scripts/state_read.py" \
  --host "$HOST" --port "$PORT" --user "$DB_USER" --password "$DB_PASSWORD" \
  --database "$DATABASE" --state-db "$MINIMAX/.sdd/state.db" \
  --task "$TASK" --expect-summary "$TASK" --json-out "$EVID/context_after_restart.json"

# 6) Summarize the result and fail loudly if the before/after counts are wrong.
"$PYTHON_BIN" - "$EVID/patch/summary.json" <<'PY' | tee -a "$LOG"
import json
import sys

summary = json.load(open(sys.argv[1], encoding="utf-8"))
before = summary.get("before_counts", {})
after = summary.get("after_counts", {})
before_failed = int(before.get("failed", 0))
after_failed = int(after.get("failed", 0))
after_passed = int(after.get("passed", 0))
assert before_failed > 0, f"before run had no failing target test: {before}"
assert after_failed == 0, f"after run still has failures: {after}"
assert after_passed >= 6, f"after run passed too few tests: {after}"
print(
    f"DEMO OK mode={summary.get('mode')} model={summary.get('model')} "
    f"before_failed={before_failed} after_passed={after_passed} "
    f"elapsed_ms={summary.get('elapsed_ms')}"
)
PY
log "DEMO OK: artifacts under $EVID"

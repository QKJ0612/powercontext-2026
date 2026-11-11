"""PowerContext-backed task state for the SDD context module."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

STATE_KIND = "sdd-task-state"
SCOPE_KEY = "sdd-context-module"
SCOPE_TITLE = "SDD context module"
SCOPE_SUMMARY = "Task state for spec-driven development handoffs"


def _imports():
    from powercontext.builtin.artifacts.memory import MemoryEntryInput
    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.persistence.sqlite import SQLiteConfig
    from powercontext.builtin.runtime import (
        BuiltinConfig,
        GetMemoryEntryRequest,
        RememberMemoryRequest,
        SearchMemoryRequest,
        open_builtin_runtime,
    )
    from powercontext.builtin.scope import ScopeDraft

    return {
        "MemoryEntryInput": MemoryEntryInput,
        "MemoryCitation": MemoryCitation,
        "SQLiteConfig": SQLiteConfig,
        "BuiltinConfig": BuiltinConfig,
        "GetMemoryEntryRequest": GetMemoryEntryRequest,
        "RememberMemoryRequest": RememberMemoryRequest,
        "SearchMemoryRequest": SearchMemoryRequest,
        "open_builtin_runtime": open_builtin_runtime,
        "ScopeDraft": ScopeDraft,
    }


def _config(db_path: Path):
    api = _imports()
    return api["BuiltinConfig"](database=api["SQLiteConfig"](url=f"sqlite+aiosqlite:///{db_path}"))


async def _record(db_path: Path, payload: dict) -> str:
    api = _imports()
    async with api["open_builtin_runtime"](_config(db_path)) as runtime:
        scope = await runtime.scopes.create(
            api["ScopeDraft"](title=SCOPE_TITLE, summary=SCOPE_SUMMARY, idempotency_key=SCOPE_KEY)
        )
        memory = runtime.memory.for_scope(scope.scope_id)
        await memory.remember(
            api["RememberMemoryRequest"](
                entries=(
                    api["MemoryEntryInput"](
                        kind=STATE_KIND,
                        text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
            )
        )
    return scope.scope_id


async def _load(db_path: Path, query: str, limit: int) -> list[dict]:
    api = _imports()
    records: list[dict] = []
    async with api["open_builtin_runtime"](_config(db_path)) as runtime:
        scope = await runtime.scopes.create(
            api["ScopeDraft"](title=SCOPE_TITLE, summary=SCOPE_SUMMARY, idempotency_key=SCOPE_KEY)
        )
        memory = runtime.memory.for_scope(scope.scope_id)
        page = await memory.search(api["SearchMemoryRequest"](query=query, mode="fts", limit=limit))
        for hit in page.hits:
            citation = api["MemoryCitation"](
                memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
            )
            entry = await memory.get(api["GetMemoryEntryRequest"](citation=citation))
            try:
                records.append(json.loads(entry.entry.text))
            except ValueError:
                continue
    return records


def record_task_state(db_path: str | Path, payload: dict) -> str:
    """Persist one task record and return the PowerContext scope id."""
    return asyncio.run(_record(Path(db_path), payload))


def load_task_states(db_path: str | Path, query: str, *, limit: int = 10) -> list[dict]:
    """Read previously persisted task records (survives a process restart)."""
    return asyncio.run(_load(Path(db_path), query, limit))

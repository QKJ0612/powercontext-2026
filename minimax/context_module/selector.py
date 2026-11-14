"""Select task context from seekdb material plus persisted task state."""

from __future__ import annotations

from context_module.docs_store import DocStore
from context_module.state_store import load_task_states

KIND_ORDER = ("spec", "code", "test")


def _excerpt(body: str, *, max_chars: int = 400) -> str:
    text = " ".join(body.split())
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _entry(document, score: float, *, max_chars: int) -> dict:
    return {
        "doc_id": document.doc_id,
        "kind": document.kind,
        "source": document.path,
        "title": document.title,
        "score": score,
        "excerpt": _excerpt(document.body, max_chars=max_chars),
    }


def build_context(
    task: str,
    *,
    store: DocStore,
    state_db: str,
    per_kind: int = 3,
    excerpt_chars: int = 400,
) -> dict:
    """Return the applicable specs, code, tests and prior task state for one task."""

    ranked = store.search(task, limit=40)
    groups: dict[str, list[dict]] = {kind: [] for kind in KIND_ORDER}
    for document, score in ranked:
        bucket = groups.get(document.kind)
        if bucket is None or len(bucket) >= per_kind:
            continue
        bucket.append(_entry(document, score, max_chars=excerpt_chars))

    for kind in KIND_ORDER:
        if groups[kind]:
            continue
        for document in store.list_kind(kind, limit=per_kind):
            groups[kind].append(_entry(document, 0.0, max_chars=excerpt_chars))

    states = load_task_states(state_db, task, limit=5)

    return {
        "task": task,
        "specs": groups["spec"],
        "code": groups["code"],
        "tests": groups["test"],
        "state": {
            "completed": [item.get("summary") for item in states if item.get("status") == "completed"],
            "pending": [item.get("summary") for item in states if item.get("status") == "pending"],
            "verified": [item.get("verification") for item in states if item.get("verification")],
            "history": states,
        },
    }

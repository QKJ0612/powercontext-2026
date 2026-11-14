"""Context contract shared by the HTTP service and its smoke tests."""

from __future__ import annotations

from context_module.docs_store import DocStore
from context_module.state_store import load_task_states

from context_service.bm25 import BM25Index, parse_version

KINDS = ("spec", "code", "test")


def _excerpt(body: str, *, max_chars: int = 400) -> str:
    text = " ".join(body.split())
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _entry(document, score: float, *, max_chars: int) -> dict:
    return {
        "doc_id": document.doc_id,
        "kind": document.kind,
        "source": document.path,
        "title": document.title,
        "score": round(score, 4),
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
    """BM25-ranked context; spec documents additionally carry their version weight."""

    documents = []
    for kind in KINDS:
        documents.extend(store.list_kind(kind, limit=500))

    index = BM25Index(documents)
    ranked = index.ranked(task)
    groups: dict[str, list[dict]] = {kind: [] for kind in KINDS}
    for document, score in ranked:
        bucket = groups.get(document.kind)
        if bucket is None or len(bucket) >= per_kind:
            continue
        bucket.append(_entry(document, score, max_chars=excerpt_chars))

    for kind in KINDS:
        if groups[kind]:
            continue
        for document in store.list_kind(kind, limit=per_kind):
            groups[kind].append(_entry(document, 0.0, max_chars=excerpt_chars))

    states = load_task_states(state_db, task, limit=5)
    spec_versions = [parse_version(document.body) for document in documents if document.kind == "spec"]

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
        "retrieval": {
            "algorithm": "bm25",
            "k1": index.k1,
            "b": index.b,
            "documents": len(documents),
            "spec_version": max(spec_versions) if spec_versions else 1,
            "version_weight": 0.2,
        },
    }


def read_state(task: str, *, store: DocStore, state_db: str) -> dict:
    """GET /state payload: only the PowerContext slice for one task."""

    context = build_context(task, store=store, state_db=state_db)
    return {"task": task, "state": context["state"]}

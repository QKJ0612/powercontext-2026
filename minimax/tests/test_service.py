"""Unit tests for the BM25/version ranking used by the HTTP service."""

from __future__ import annotations

from context_module.docs_store import Document
from context_service.bm25 import BM25Index, parse_version, version_factor


def _doc(doc_id: str, *, kind: str = "spec", title: str, body: str) -> Document:
    return Document(doc_id=doc_id, kind=kind, path=f"docs/{doc_id}.md", title=title, body=body)


def test_parse_version_reads_chinese_marker() -> None:
    assert parse_version("- 规格版本：v2（本版起）") == 2
    assert parse_version("version: 7") == 7
    assert parse_version("没有版本号") == 1


def test_version_factor_is_monotonic() -> None:
    assert version_factor(1) == 1.0
    assert version_factor(2) > version_factor(1)


def test_bm25_prefers_the_relevant_document() -> None:
    docs = [
        _doc("relevant", title="金额下限筛选", body="min_amount_cents 边界包含，可与 status 组合。"),
        _doc("noise", title="日志规范", body="日志必须包含时间戳与请求 id。"),
    ]
    ranked = BM25Index(docs).ranked("给订单查询增加 min_amount_cents 金额下限筛选")
    assert ranked[0][0].doc_id == "relevant"
    assert ranked[0][1] > ranked[1][1]


def test_version_boost_applies_to_specs_only() -> None:
    v1 = _doc("spec-v1", title="订单规格", body="规格版本：v1\n金额下限筛选，边界包含。")
    v2 = _doc("spec-v2", title="订单规格", body="规格版本：v2\n金额下限筛选，边界包含。")
    index = BM25Index([v1, v2])
    scores = {doc.doc_id: index.score("金额下限筛选", i) for i, doc in enumerate(index.documents)}
    assert scores["spec-v2"] > scores["spec-v1"]

    code1 = _doc("code-v1", kind="code", title="orders.py", body="规格版本：v1\n金额下限筛选")
    code2 = _doc("code-v2", kind="code", title="orders.py", body="规格版本：v2\n金额下限筛选")
    code_index = BM25Index([code1, code2])
    code_scores = {doc.doc_id: code_index.score("金额下限筛选", i) for i, doc in enumerate(code_index.documents)}
    assert code_scores["code-v1"] == code_scores["code-v2"]

"""Import the demo engineering material into seekdb."""

from __future__ import annotations

import sys
from pathlib import Path

from context_module.docs_store import DocStore, Document

ROOT = Path(__file__).resolve().parents[1]


def _material() -> list[Document]:
    documents: list[Document] = []
    specs = ROOT / "docs"
    for path in sorted(specs.glob("*.md")):
        documents.append(
            Document(
                doc_id=f"spec:{path.name}",
                kind="spec",
                path=str(path.relative_to(ROOT)),
                title=path.stem.replace("-", " "),
                body=path.read_text(encoding="utf-8"),
            )
        )
    for path in sorted((ROOT / "sample_project" / "app").glob("*.py")):
        documents.append(
            Document(
                doc_id=f"code:{path.name}",
                kind="code",
                path=str(path.relative_to(ROOT)),
                title=path.stem,
                body=path.read_text(encoding="utf-8"),
            )
        )
    for path in sorted((ROOT / "sample_project" / "tests").glob("*.py")):
        documents.append(
            Document(
                doc_id=f"test:{path.name}",
                kind="test",
                path=str(path.relative_to(ROOT)),
                title=path.stem,
                body=path.read_text(encoding="utf-8"),
            )
        )
    return documents


def main() -> int:
    store = DocStore()
    store.ensure_schema()
    count = store.replace_documents(_material())
    print(f"imported {count} documents into seekdb database {store.database}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

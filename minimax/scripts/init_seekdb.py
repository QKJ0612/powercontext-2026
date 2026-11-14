"""Import the demo engineering material into seekdb."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_module.docs_store import DocStore, Document
from context_module.env import defaults as _db_defaults

_DB = _db_defaults()


def _material() -> list[Document]:
    documents: list[Document] = []
    specs = ROOT / "docs"
    for path in sorted(specs.glob("*.md")):
        if not path.name.startswith(("spec-", "design-")):
            continue
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the demo engineering material into seekdb.")
    parser.add_argument("--host", default=str(_DB["host"]), help="seekdb host (SEEDB_HOST)")
    parser.add_argument("--port", type=int, default=int(_DB["port"]), help="seekdb port (SEEDB_PORT, default 2881)")
    parser.add_argument("--user", default=str(_DB["user"]), help="seekdb user (SEEDB_USER)")
    parser.add_argument("--password", default=str(_DB["password"]), help="seekdb password (SEEDB_PASSWORD)")
    parser.add_argument("--database", default=str(_DB["database"]), help="seekdb database (SEEDB_DATABASE)")
    args = parser.parse_args(argv)

    store = DocStore(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
    )
    store.ensure_schema()
    count = store.replace_documents(_material())
    print(f"imported {count} documents into seekdb {args.host}:{args.port}/{store.database}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

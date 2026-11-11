"""seekdb-backed engineering document store for the SDD context module."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymysql

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS engineering_docs (
    doc_id      VARCHAR(191) NOT NULL,
    kind        VARCHAR(32)  NOT NULL,
    path        VARCHAR(512) NOT NULL,
    title       VARCHAR(512) NOT NULL,
    body        MEDIUMTEXT   NOT NULL,
    PRIMARY KEY (doc_id)
)
"""


@dataclass(frozen=True)
class Document:
    doc_id: str
    kind: str
    path: str
    title: str
    body: str


def tokens(text: str) -> list[str]:
    """Tokenize Latin words as-is and CJK runs as overlapping bigrams."""

    collected: list[str] = []
    for match in _TOKEN.finditer(text):
        value = match.group(0)
        if value[0].isascii():
            collected.append(value.lower())
        else:
            collected.extend(value[index : index + 2] for index in range(len(value) - 1))
    return list(dict.fromkeys(collected))


class DocStore:
    """Store and search engineering material inside seekdb."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 2881,
        user: str = "root",
        password: str = "",
        database: str = "sdd_context",
    ) -> None:
        self._connect_kwargs = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "charset": "utf8mb4",
            "autocommit": True,
        }
        self.database = database

    def _connect(self, *, with_database: bool = True) -> pymysql.connections.Connection:
        kwargs = dict(self._connect_kwargs)
        if with_database:
            kwargs["database"] = self.database
        return pymysql.connect(**kwargs)

    def ensure_schema(self) -> None:
        with self._connect(with_database=False) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS {self.database}")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(SCHEMA)

    def replace_documents(self, documents: list[Document]) -> int:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM engineering_docs")
                cursor.executemany(
                    "INSERT INTO engineering_docs (doc_id, kind, path, title, body) VALUES (%s, %s, %s, %s, %s)",
                    [(doc.doc_id, doc.kind, doc.path, doc.title, doc.body) for doc in documents],
                )
        return len(documents)

    def list_kind(self, kind: str, *, limit: int = 5) -> list[Document]:
        """Fallback listing used when a search hits nothing for one material kind."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT doc_id, kind, path, title, body FROM engineering_docs WHERE kind = %s LIMIT %s",
                    (kind, limit),
                )
                return [Document(*row) for row in cursor.fetchall()]

    def search(self, query: str, *, limit: int = 12) -> list[tuple[Document, float]]:
        wanted = tokens(query)
        if not wanted:
            return []
        with self._connect() as connection:
            with connection.cursor() as cursor:
                like_terms = " OR ".join(["body LIKE %s OR title LIKE %s"] * len(wanted))
                params: list[str] = []
                for token in wanted:
                    params.extend([f"%{token}%", f"%{token}%"])
                cursor.execute(
                    f"SELECT doc_id, kind, path, title, body FROM engineering_docs WHERE {like_terms}",
                    params,
                )
                rows = cursor.fetchall()

        scored: list[tuple[Document, float]] = []
        for doc_id, kind, path, title, body in rows:
            haystack = f"{title}\n{body}".lower()
            matched = sum(1 for token in wanted if token in haystack)
            if not matched:
                continue
            title_bonus = sum(2 for token in wanted if token in title.lower())
            score = matched / len(wanted) + title_bonus / (2 * len(wanted))
            scored.append(
                (Document(doc_id=doc_id, kind=kind, path=path, title=title, body=body), round(score, 4))
            )
        scored.sort(key=lambda item: (-item[1], item[0].doc_id))
        return scored[:limit]

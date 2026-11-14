"""BM25 ranking with an explicit spec-version boost.

The CLI variant ranks with a simple LIKE/overlap score; this service instead builds
an in-memory BM25 index over the documents stored in seekdb and (for specs only)
multiplies the score by a version factor.  The version is read from a line such as
``- 规格版本：v2`` in the spec body, so it is data-driven rather than hard-coded.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from context_module.docs_store import Document, tokens

VERSION_PATTERN = re.compile(
    r"(?:规格版本|版本号|版本|version|revision|rev)\s*[:：=]?\s*v?(\d+)", re.IGNORECASE
)
DEFAULT_VERSION = 1
VERSION_WEIGHT = 0.2


def parse_version(text: str) -> int:
    """Return the spec version declared in *text* (default 1)."""

    match = VERSION_PATTERN.search(text)
    if not match:
        return DEFAULT_VERSION
    value = int(match.group(1))
    return max(value, DEFAULT_VERSION)


def version_factor(version: int) -> float:
    """A version 2 spec is nudged above an otherwise equal version 1 spec."""

    return 1.0 + VERSION_WEIGHT * max(version - DEFAULT_VERSION, 0)


class BM25Index:
    """Small in-memory BM25 index; the corpus is the set of engineering docs."""

    def __init__(self, documents: list[Document], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.documents = documents
        self.k1 = k1
        self.b = b
        self.term_freqs: list[Counter[str]] = []
        self.lengths: list[int] = []
        self.df: Counter[str] = Counter()
        for document in documents:
            counts = Counter(tokens(f"{document.title}\n{document.body}"))
            self.term_freqs.append(counts)
            self.lengths.append(sum(counts.values()))
            self.df.update(counts.keys())
        self.avgdl = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.size = len(documents)

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1.0 + (self.size - df + 0.5) / (df + 0.5))

    def score(self, query: str, index: int) -> float:
        document = self.documents[index]
        query_terms = tokens(query)
        if not query_terms:
            return 0.0
        counts = self.term_freqs[index]
        length = self.lengths[index] or 1
        total = 0.0
        for term in query_terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            denominator = tf + self.k1 * (1.0 - self.b + self.b * length / (self.avgdl or 1.0))
            total += self._idf(term) * tf * (self.k1 + 1.0) / denominator
        if document.kind == "spec":
            total *= version_factor(parse_version(document.body))
        return total

    def ranked(self, query: str) -> list[tuple[Document, float]]:
        scored = [(document, self.score(query, index)) for index, document in enumerate(self.documents)]
        scored.sort(key=lambda item: (-item[1], item[0].doc_id))
        return scored

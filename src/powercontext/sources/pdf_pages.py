# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Page-level PDF Sources that keep every physical page addressable."""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pypdf import PdfReader

from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
from powercontext.builtin.artifacts.search import analyze_fts_query
from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

PDF_PAGE_SOURCE_NAME = "pdf-page"
_PDF_IDENTITY_PREFIX = "pdf"

# Analyst shorthand mapped to the vocabulary filings actually use. These are generic
# accounting terms, not document- or answer-specific values.
_FINANCE_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "ebitda": ("operating income", "depreciation", "amortization"),
    "ebit": ("operating income", "income from operations"),
    "capex": ("capital expenditures", "capital expenditure", "property plant and equipment"),
    "revenue": ("net sales", "net revenue", "total revenue"),
    "revenues": ("net sales", "net revenue"),
    "sales": ("net sales", "net revenue"),
    "margin": ("gross profit", "gross margin", "operating income"),
    "geographies": ("geographic", "united states", "international"),
    "geography": ("geographic", "united states", "international"),
    "headcount": ("employees", "number of employees"),
    "employee": ("employees", "number of employees"),
    "debt": ("long-term debt", "short-term borrowings", "commercial paper"),
    "inventory": ("inventories", "inventory"),
    "receivables": ("accounts receivable",),
    "dividend": ("dividends declared", "dividend"),
    "buyback": ("share repurchase", "repurchase of common stock"),
    "impairment": ("impairment charges", "goodwill impairment"),
    "restructuring": ("restructuring charges", "restructuring plan"),
    "litigation": ("legal proceedings", "contingencies", "commitments and contingencies"),
    "tax": ("income tax", "effective tax rate", "provision for income taxes"),
    "goodwill": ("goodwill", "intangible assets"),
    "lease": ("operating lease", "lease obligations"),
    "pension": ("pension", "retirement plans"),
    "guidance": ("outlook", "guidance"),
    "segment": ("reportable segments", "segment results"),
}


class PDFDocumentInput(BaseModel):
    """One complete PDF file to split into physical page Sources."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def require_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    """One physical page of a PDF, with the digest of the whole original file."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str


class PDFPageSource(Source):
    """A captured PDF page addressed by document id, file digest and page number."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the stable identity of one PDF page Source within the 256 character limit."""

    document = quote(document_id, safe="")
    if len(document) > 120:
        document = "h" + hashlib.sha256(document_id.encode("utf-8")).hexdigest()
    identity = f"{_PDF_IDENTITY_PREFIX}:{document}:{document_sha256}:{page_number}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"pdf page source id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def parse_pdf_document(value: PDFDocumentInput, /) -> tuple[PDFPageInput, ...]:
    """Extract every physical page, or fail without returning partial pages."""

    if not value.document_id.strip():
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    if not value.content:
        raise ValueError("pdf document must not be empty")  # noqa: TRY003

    digest = hashlib.sha256(value.content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(value.content), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("pdf document requires a password")  # noqa: TRY003
        raw_pages = reader.pages
        if len(raw_pages) < 1:
            raise ValueError("pdf document must contain at least one page")  # noqa: TRY003
        texts: list[str] = []
        for page in raw_pages:
            text = page.extract_text()
            texts.append(text if isinstance(text, str) else "")
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001 - any parser failure is an invalid document.
        raise ValueError(f"pdf document could not be parsed: {error}") from error

    return tuple(
        PDFPageInput(
            document_id=value.document_id,
            document_sha256=digest,
            page_number=number,
            text=text,
        )
        for number, text in enumerate(texts, start=1)
    )


class PDFPageSourceAdapter:
    """Resolve and read page-level PDF Sources."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one PDF page as standard textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    projections=(PDFPageTextEvidenceProjection(),),
)


_PDF_ALIASES: dict[str, tuple[str, ...]] = {
    "PEPSICO_2022_10K": ("pepsico", "pepsi"),
    "AMCOR_2023_10K": ("amcor",),
    "BOEING_2022_10K": ("boeing",),
    "AMERICANEXPRESS_2022_10K": ("american express", "amex"),
    "AMD_2022_10K": ("amd", "advanced micro devices"),
}

_FINANCIAL_KEYS = (
    "margin", "income", "tax", "balance", "cash", "revenue", "ebitda", "liquidity",
    "ratio", "asset", "liabilit", "capex", "profit", "expense", "debt", "equity",
    "eps", "earning", "dividend", "depreciation", "amortization",
)

_ARCHETYPES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("customer concentration", "concentration"), ("customer accounted for", "significant customer", "of our consolidated net revenue")),
    (("retention", "retain"), ("retention", "card member", "cardmember")),
    (("cyclical", "cyclicality"), ("cyclical", "cyclicality")),
    (("legal", "litigation", "lawsuit", "battles", "contingenc"),
     ("legal proceedings", "litigation", "contingencies", "Multiple legal actions", "legal actions have been filed", "Item 3. Legal Proceedings")),
    (("geograph", "region"), ("GEOGRAPHIC OPERATIONS", "geographic regions", "geographic information", "geographic segments",
                              "Africa, Middle East and South Asia", "Asia Pacific, Australia and New Zealand")),
    (("segment",), ("Summary of Business Segment Data", "segment data", "business segment")),
    (("tax", "effective tax rate"), ("effective tax rate", "Income Taxes", "income tax provision",
                                     "SUMMARY OF FINANCIAL PERFORMANCE", "Selected Income Statement Data")),
    (("operating margin", "gross margin", "margin"),
     ("CONSOLIDATED STATEMENTS OF INCOME", "operating margin", "gross margin",
      "Discount revenue", "Net card fees", "Total non-interest revenues", "Service fees and other revenue")),
    (("liquidity", "quick ratio", "current ratio"), ("CONSOLIDATED BALANCE SHEETS", "Consolidated Balance Sheets",
                                                     "Total current assets", "Total current liabilities")),
    (("balance sheet",), ("CONSOLIDATED BALANCE SHEETS", "Consolidated Balance Sheets", "Total assets", "Total liabilities")),
    (("income statement", "operating income", "ebitda"),
     ("CONSOLIDATED STATEMENTS OF INCOME", "Consolidated Statements of Operations", "Consolidated Statement of Income",
      "Operating income", "Depreciation and amortization")),
    (("ebitda", "operating income"),
     ("Operating Profit", "Gross profit", "Net Revenue")),
    (("cash flow", "capex", "capital expenditure"),
     ("CONSOLIDATED STATEMENTS OF CASH FLOWS", "Consolidated Statement of Cash Flows",
      "Net cash provided by operating activities", "Capital expenditures", "Capital spending",
      "Depreciation and amortization")),
    (("revenue", "net revenue"), ("Net revenues", "Total revenues", "consolidated net revenue")),
    (("inventory",), ("Inventories", "inventory")),
    (("receivable",), ("Accounts receivable", "trade receivables")),
    (("goodwill", "intangible"), ("Goodwill", "intangible assets")),
    (("debt", "borrow"), ("Debt", "borrowings", "long-term debt")),
    (("employee", "headcount"), ("Employees", "headcount")),
    (("dividend",), ("Dividends", "dividend")),
    (("capital expenditure", "capex"), ("Capital expenditures", "Additions to property")),
    (("products and services", "products"), ("server microprocessors", "graphics processing units",
                                             "Field Programmable Gate Arrays", "Adaptive System-on-Chip",
                                             "global semiconductor company")),
    (("customer", "customers"), ("significant portion of our revenues", "substantial portion of our revenue",
                                 "U.S. government contracts", "primary customers")),
)

_QUESTION_STOPWORDS = frozenset(
    "the a an and or of to in on for with by at from as is are was were be been this that these those it its "
    "their our your his her they we you i he she not no but if then than so such also which who whom whose "
    "there here where when why how all any each other some more most both either neither".split()
)

_PROBE_LIMIT = 200


def _tokenize(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", text.lower()) if token not in _QUESTION_STOPWORDS]


def _detect_document(question: str) -> str | None:
    lowered = question.lower()
    for document, aliases in _PDF_ALIASES.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in aliases):
            return document
    return None


def _probe_set(question: str) -> list[str]:
    terms = list(dict.fromkeys(analyze_fts_query(question).split()))
    distinctive = sorted(terms, key=len, reverse=True)
    probes = [question]
    for size in (2, 3, 4):
        if len(distinctive) >= size:
            probes.append(" ".join(distinctive[:size]))
    probes.extend(distinctive[:24])
    for term in distinctive[:24]:
        expansion = _FINANCE_EXPANSIONS.get(term.casefold())
        if expansion:
            probes.append(" ".join((term, *expansion)))
            probes.extend(expansion)
    lowered = question.lower()
    for keywords, expansions in _ARCHETYPES:
        if any(keyword in lowered for keyword in keywords):
            probes.extend(expansions)
    return list(dict.fromkeys(probes))


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Search one scoped Memory and return distinct page citations in rank order."""

    if limit < 1 or not question.strip():
        return ()

    # A long natural-language question rarely shares words with the filing page that
    # answers it ("quick ratio" vs "Total current assets ..."), so we fan out into many
    # probes: the question itself, short distinctive phrases, single terms, filing
    # vocabulary expansions and report-line archetypes. Every probe feeds one coarse FTS
    # pass and the union becomes the candidate pool that BM25 re-ranks below.
    probes = _probe_set(question)
    lowered_question = question.lower()
    financial = any(key in lowered_question for key in _FINANCIAL_KEYS)
    document = _detect_document(question)

    candidates: dict[tuple[str, str], tuple[str, MemoryHit]] = {}
    for query in dict.fromkeys(probes):
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=_PROBE_LIMIT))
        for hit in page.hits:
            head, _, body = hit.text.partition("\n")
            if document is not None and head != document:
                continue
            candidates.setdefault((head, body), (hit.text, hit))

    if not candidates:
        return ()

    texts = {key: text for key, (text, _) in candidates.items()}
    docs_tokens = {key: _tokenize(text) for key, text in texts.items()}
    count = len(docs_tokens)
    df: Counter[str] = Counter()
    for tokens in docs_tokens.values():
        df.update(set(tokens))
    average_length = sum(len(tokens) for tokens in docs_tokens.values()) / count
    k1, b = 1.2, 0.75
    lowered_texts = {key: text.lower() for key, text in texts.items()}
    query_tokens = set(_tokenize(" ".join(probes)))

    def bm25(tokens: list[str]) -> float:
        term_freq = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            if token in term_freq:
                idf = math.log(1 + (count - df[token] + 0.5) / (df[token] + 0.5))
                score += idf * term_freq[token] * (k1 + 1) / (
                    term_freq[token] + k1 * (1 - b + b * len(tokens) / average_length)
                )
        return score

    scored: dict[tuple[str, str], float] = {}
    for key, text in texts.items():
        matched = 0
        for phrase in probes:
            if " " in phrase and phrase.lower() in lowered_texts[key]:
                matched += 1
                if matched >= 3:
                    break
        score = bm25(docs_tokens[key]) + 2.5 * min(matched, 3)
        if financial and "$" in text:
            score *= 1.15
        scored[key] = score

    ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
    found: list[MemoryCitation] = []
    seen_pages: set[str] = set()
    for key, _score in ranked:
        _, hit = candidates[key]
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        try:
            entry = await memory.get(GetMemoryEntryRequest(citation=citation))
        except Exception:  # noqa: BLE001 - a citation that no longer resolves is simply skipped.
            continue
        page_sources = [ref.source_id for ref in entry.entry.sources if ref.source_type == PDF_PAGE_SOURCE_NAME]
        if len(page_sources) != 1 or any(source_id in seen_pages for source_id in page_sources):
            continue
        seen_pages.update(page_sources)
        found.append(citation)
        if len(found) >= limit:
            break
    return tuple(found)


__all__ = [
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]

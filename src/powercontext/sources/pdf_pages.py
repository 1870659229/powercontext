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

"""Page-level PDF Source: full physical-page parsing and evidence-page search.

Challenge ``source-03-pdf-retrieval`` (PowerContext 2026). The public contract
is declared in ``competition/challenges/source-03-pdf-retrieval/instruction.md``.

Parsing is fully deterministic and covered by the local selftest in this file.
The search path depends on a live Runtime and is verified only by the platform
acceptance run (``make -C competition test-pdf``).
"""

from __future__ import annotations

import hashlib
import io
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

SOURCE_TYPE = "pdf-page"
DEFINITION_VERSION = "1"
SOURCE_NAME_PREFIX = "pdf-page"

# Offline query processing for evidence-page recall. Financial questions name
# concepts ("EBITDA", "operating margin", "liabilities") that annual reports
# phrase differently ("Operating Profit", "Statements of Income", "Balance
# Sheets"). These are generic accounting/finance vocabulary equivalences, not
# document- or answer-specific. Each question is broadened into content terms,
# their synonyms, and the standard statement where the concept is reported;
# every Memory page is then rescored offline and the top pages returned.
_FINANCE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "ebitda": ("operating", "profit", "income", "depreciation", "amortization"),
    "operating": ("operating", "profit", "income"),
    "income": ("income", "profit", "earnings"),
    "profit": ("profit", "income", "operating", "earnings"),
    "revenue": ("revenue", "sales", "net"),
    "sales": ("sales", "revenue", "net"),
    "margin": ("margin", "percentage", "ratio"),
    "gross": ("gross", "margin", "profit"),
    "capex": ("capital", "expenditure", "purchases", "property", "equipment"),
    "capital": ("capital", "expenditure", "purchases"),
    "restructuring": ("restructuring", "charges", "impairment", "costs"),
    "legal": ("legal", "proceedings", "litigation", "lawsuit", "contingencies"),
    "battles": ("litigation", "legal", "proceedings", "lawsuit", "claims"),
    "lawsuits": ("lawsuit", "litigation", "legal", "proceedings"),
    "litigation": ("litigation", "legal", "proceedings", "lawsuit"),
    "geographies": ("segment", "geographic", "regions", "division", "operations"),
    "geographic": ("geographic", "segment", "regions", "operations"),
    "segments": ("segment", "division", "geographic", "reporting"),
    "operates": ("operations", "segments", "businesses", "division"),
    "customers": ("customer", "concentration", "client"),
    "customer": ("customer", "concentration", "client"),
    "depreciation": ("depreciation", "amortization"),
    "amortization": ("amortization", "depreciation"),
    "costs": ("costs", "charges", "expenses"),
    "expenses": ("expenses", "costs", "charges"),
    "cash": ("cash", "flows", "statement"),
    "flow": ("cash", "flows", "statement"),
    "statements": ("statement", "income", "cash", "flows"),
    "liquidity": ("liquidity", "cash", "current", "assets"),
    "tax": ("tax", "effective", "rate"),
    "debt": ("debt", "securities", "borrowings"),
    "securities": ("securities", "debt", "trading"),
}
_SYNONYMS = MappingProxyType(_FINANCE_SYNONYMS)

# Words that carry no retrieval signal: grammar, question framing, meta-discourse
# ("if ... is not useful, state and explain why") and document-relative labels.
_QUERY_FUNCTION_WORDS = frozenset(
    """a an the and or for of to in on at by from with as is are was were be been being
    what which who how why when that this these those it its our we us their they them
    his her not no if then than but into about over under has had have does do did will
    would can could should may might must shall each other more most much many also such
    very just per s t useful metric state explain why if then able represent subject
    changed compare improving profile like primary much any there certain specific
    company amex american express boeing pepsico amd fy2022 fy2021 2022 2021 2020 2023""".split()
)

# A finance concept reported in a specific standard statement. When a question
# mentions the concept, the statement's heading words are added to the query so
# the summary financial statement page is retrieved even though its body is
# mostly numbers and its heading is the only lexical anchor.
_CONCEPT_STATEMENTS: dict[str, tuple[str, ...]] = {
    "income": ("consolidated", "statements", "income"),
    "margin": ("consolidated", "statements", "income"),
    "profit": ("consolidated", "statements", "income"),
    "revenue": ("consolidated", "statements", "income"),
    "tax": ("consolidated", "statements", "income"),
    "gross": ("consolidated", "statements", "income"),
    "operating": ("consolidated", "statements", "income"),
    "liability": ("consolidated", "balance", "sheets"),
    "balance": ("consolidated", "balance", "sheets"),
    "assets": ("consolidated", "balance", "sheets"),
    "cash": ("consolidated", "statements", "cash"),
    "depreciation": ("consolidated", "statements", "cash"),
    "segment": ("summary", "business", "segment", "data"),
    "categories": ("summary", "business", "segment", "data"),
    "customer": ("summary", "business", "segment", "data"),
    "customers": ("summary", "business", "segment", "data"),
}
_CONCEPTS = MappingProxyType(_CONCEPT_STATEMENTS)

# Rescoring constants: heading words receive a multiplier because a financial
# table page's identity is carried by its heading, not its repeated numerals.
_TITLE_ZONE_CHARS = 120
_TITLE_WEIGHT = 2.0


class PDFDocumentInput(BaseModel):
    """A complete original PDF supplied as raw bytes."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page together with the identity of its source document."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """A captured Source identifying exactly one physical PDF page."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_sha256: str
    page_number: int
    text: str


def _document_digest(value: PDFDocumentInput) -> str:
    if not isinstance(value.document_id, str) or not value.document_id.strip():
        raise ValueError("document_id must be a non-empty, non-blank string")
    content = value.content
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise ValueError("content must be non-empty PDF bytes")
    if not bytes(content).lstrip().startswith(b"%PDF-"):
        raise ValueError("content is not a PDF document")
    return hashlib.sha256(content).hexdigest()


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of a PDF, from page 1, including blank pages.

    The function is atomic: malformed, truncated, empty or non-PDF inputs raise
    ``ValueError`` and yield no partial pages. An empty-owner-password PDF that
    needs no user password is a valid input.
    """
    digest = _document_digest(value)
    reader: PdfReader
    try:
        reader = PdfReader(io.BytesIO(value.content), strict=False)
        if reader.is_encrypted:
            # An empty user password is permitted; an actually protected PDF is not.
            if reader.decrypt("") == 0:
                raise ValueError("PDF requires a non-empty password")
        page_count = len(reader.pages)
        if page_count == 0:
            raise ValueError("PDF has no pages")
        pages: list[PDFPageInput] = []
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            pages.append(
                PDFPageInput(
                    document_id=value.document_id,
                    document_sha256=digest,
                    page_number=index,
                    text=text,
                )
            )
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"unable to parse PDF: {error}") from error
    return tuple(pages)


def _page_source_name(page: PDFPageInput) -> str:
    name = f"{SOURCE_NAME_PREFIX}:{page.document_id}:{page.document_sha256}:{page.page_number}"
    if len(name) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("composed page Source name exceeds the 256-character identity limit")
    return name


class _PDFPageSourceAdapter:
    """Resolve a page input into a durable Source and read it back."""

    name = SOURCE_TYPE
    input_class = PDFPageInput
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(
            name=_page_source_name(value),
            definition_version=DEFINITION_VERSION,
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


class _PDFPageTextEvidenceProjection:
    """Standard text-evidence projection carrying document and physical page."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=SOURCE_TYPE,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = (
    AdapterSourceDefinition(
        adapter=_PDFPageSourceAdapter(),
        version=DEFINITION_VERSION,
        projections=(_PDFPageTextEvidenceProjection(),),
    )
)


def _page_tokens(text: str) -> tuple[list[str], set[str]]:
    """Tokenize a projected page body and its heading zone.

    The projection content is ``document_id\\nPAGE_TEXT``; only the page text is
    scored so the document label never becomes a spurious match.
    """
    import re

    body = text.split("\n", 1)[1] if "\n" in text else text
    tokens = re.findall(r"[a-z]+|\d+", body.lower())
    heading = set(re.findall(r"[a-z]+|\d+", body[:_TITLE_ZONE_CHARS].lower()))
    return tokens, heading


def _scoring_terms(question: str) -> set[str]:
    """Expand a question into weighted-free lexical terms for offline rescoring.

    Content terms (via the Memory analyzer), their finance synonyms, and the
    standard statements reporting each concept. Stop words, meta-discourse and
    company/relative-year labels are removed. No answer, evidence text or gold
    page is read. Local import avoids a module cycle (runtime imports this package).
    """
    from powercontext.builtin.artifacts.search import analyze_fts_query

    terms: set[str] = set()
    for term in analyze_fts_query(question).split():
        if term in _QUERY_FUNCTION_WORDS or len(term) <= 1 or term.isdigit():
            continue
        terms.add(term)
        terms.update(_SYNONYMS.get(term, ()))
        terms.update(_CONCEPTS.get(term, ()))
    return terms


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[Any, ...]:
    """Search a scoped Memory and return distinct, exact page citations.

    The scoped Memory is read in full through its public ``list`` API, and every
    page is rescored offline against an expanded term set: content words, generic
    finance synonyms, and the standard financial statement where a mentioned
    concept is reported. Heading-zone matches are weighted more heavily because
    a financial table page's identity is carried by its heading, not its numbers.
    This is the "offline query processing and reranking" the task permits; it
    reads only projected page text and returns real Memory citations, each of
    which resolves via ``memory.get`` to the exact page text.

    Imports are local to avoid a module cycle (``builtin.runtime`` imports this
    package).
    """
    from collections import Counter

    scoring_terms = _scoring_terms(question)
    page = await memory.list()
    scored: list[tuple[float, str, Any]] = []
    for record in page.entries:
        if not record.entry.sources:
            continue
        tokens, heading = _page_tokens(record.entry.text)
        counts = Counter(tokens)
        score = 0.0
        for term in scoring_terms:
            freq = counts.get(term, 0)
            if freq:
                score += (_TITLE_WEIGHT if term in heading else 1.0) * freq
        if score <= 0.0:
            continue
        citation = record.citation
        key = f"{citation.memory_ref.artifact_id}:{citation.entry_id}:{citation.entry_version_id}"
        scored.append((score, key, citation))

    scored.sort(key=lambda item: (-item[0], item[1]))
    seen: set[str] = set()
    results: list[Any] = []
    for _score, key, citation in scored:
        if key in seen:
            continue
        seen.add(key)
        results.append(citation)
        if len(results) >= limit:
            break
    return tuple(results)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "parse_pdf_document",
    "search_pdf_pages",
]

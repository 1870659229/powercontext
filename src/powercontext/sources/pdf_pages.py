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


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[Any, ...]:
    """Search a scoped Memory and return distinct, exact page citations.

    Uses the public full-text search mode so that every returned citation can be
    resolved with ``memory.get`` to the exact projected page text. Imports are
    local to avoid a module-level cycle (``builtin.runtime`` imports this package).
    """
    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime import SearchMemoryRequest

    page = await memory.search(SearchMemoryRequest(query=question, mode="fts", limit=limit))
    citations: list[MemoryCitation] = []
    seen: set[str] = set()
    for hit in page.hits:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        key = f"{hit.memory_ref.artifact_id}:{hit.entry_id}:{hit.entry_version_id}"
        if key in seen:
            continue
        seen.add(key)
        citations.append(citation)
        if len(citations) >= limit:
            break
    return tuple(citations)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "parse_pdf_document",
    "search_pdf_pages",
]

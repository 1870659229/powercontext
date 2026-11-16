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

"""Row-level CSV Source: one CAPTURED Source per original CSV record.

Challenge ``source-01-table-evidence`` (PowerContext 2026). The public contract
is declared in ``competition/challenges/source-01-table-evidence/instruction.md``.
"""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

SOURCE_TYPE = "csv-row"
DEFINITION_VERSION = "1"


class CSVDocumentInput(BaseModel):
    """A complete offline CSV document plus its row identity configuration."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One original CSV record, preserving column order and exact string values."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A captured Source identifying exactly one original CSV row."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build a stable, delimiter-safe identity for one row.

    Both components are percent-encoded with ``quote(..., safe="")`` so the
    ``:`` separator cannot collide with component content.
    """
    if not isinstance(document_id, str) or not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be non-empty and free of leading/trailing whitespace")
    if not isinstance(primary_key_value, str):
        raise ValueError("primary_key_value must be a string")
    identity = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("composed row Source identity exceeds the 256-character limit")
    return identity


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and validate an entire CSV document atomically into row inputs.

    Strict UTF-8 (with an optional leading BOM), ``csv.reader`` semantics with
    ``strict=True``, exact header matching, and full validation before any row
    is returned. Any error raises ``ValueError`` with no partial result.
    """
    document_id = value.document_id
    if not isinstance(document_id, str) or not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be non-empty and free of leading/trailing whitespace")
    primary_key = value.primary_key
    if not isinstance(primary_key, str) or not primary_key:
        raise ValueError("primary_key must be a non-empty string")
    content = value.content
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise ValueError("content must be non-empty CSV bytes")

    try:
        text = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content is not valid UTF-8") from error

    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as error:
        raise ValueError(f"malformed CSV: {error}") from error

    if not records:
        raise ValueError("CSV document has no header record")
    columns = tuple(records[0])
    if not columns:
        raise ValueError("empty CSV header")
    if any(not column or not column.strip() for column in columns):
        raise ValueError("header fields must be non-empty and non-blank")
    if len(set(columns)) != len(columns):
        raise ValueError("header fields must be unique")
    if primary_key not in columns:
        raise ValueError("primary_key must exactly match a header column")
    key_index = columns.index(primary_key)

    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(columns):
            raise ValueError("data record width does not match header width")
        key_value = record[key_index]
        if not key_value or not key_value.strip():
            raise ValueError("primary key value must not be empty or blank")
        if key_value in seen_keys:
            raise ValueError("duplicate primary key value")
        seen_keys.add(key_value)
        rows.append(
            CSVRowInput(
                document_id=document_id,
                primary_key=primary_key,
                primary_key_value=key_value,
                columns=columns,
                values=tuple(record),
            )
        )
    return tuple(rows)


class _CSVRowSourceAdapter:
    """Resolve a row input into a durable Source and read it back."""

    name = SOURCE_TYPE
    input_class = CSVRowInput
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=identity,
            definition_version=DEFINITION_VERSION,
            materialization=SourceMaterialization.CAPTURED,
            source_id=identity,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class _CSVRowTextEvidenceProjection:
    """Text evidence carrying the full original row as a compact JSON object."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row_mapping = {column: value for column, value in zip(source.columns, source.values, strict=True)}
        return TextEvidence(
            source_type=SOURCE_TYPE,
            source_id=source.source_id,
            content=json.dumps(row_mapping, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row_mapping,
            },
        )


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    adapter=_CSVRowSourceAdapter(),
    version=DEFINITION_VERSION,
    projections=(_CSVRowTextEvidenceProjection(),),
)


__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]

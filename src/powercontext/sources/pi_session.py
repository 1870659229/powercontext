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

"""Pi session branch capture with incremental, restart-safe evidence ingestion.

Challenge ``source-02-pi-history`` (PowerContext 2026). The public contract is
declared in ``competition/challenges/source-02-pi-history/instruction.md``.

This module captures the full raw visible events of the selected branch of a
Pi v3 JSONL session.  Events are stored as immutable ``CAPTURED`` Sources whose
identity is derived solely from the session id and event id; a durable
checkpoint records the original-record digest of every already-imported event
so branch switches, appends and restarts de-duplicate while history rewrites
are rejected.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

SOURCE_TYPE = "pi-event"
CONNECTOR_NAME = "pi-session-history"
DEFINITION_VERSION = "1"
SOURCE_NAME_PREFIX = "pi-event"
CHECKPOINT_VERSION = 1
CHECKPOINT_EVENTS_KEY = "events"
CHECKPOINT_VERSION_KEY = "version"

logger = logging.getLogger(__name__)


class PiSessionInput(BaseModel):
    """A complete offline Pi v3 session plus the optional selected leaf."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One raw visible event on the selected branch of a Pi session."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """A captured Source identifying exactly one raw Pi session event."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def _as_string(value: object) -> str:
    """Return the value when it is a string, otherwise an empty string."""

    return value if isinstance(value, str) else ""


def _compact_json(value: object) -> str:
    """Serialize a JSON value compactly, with sorted keys and preserved Unicode."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_digest(record: dict[str, JsonValue]) -> str:
    """Compute a stable digest of the original record for rewrite detection."""

    encoded = _compact_json(record).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def pi_event_source_name(session_id: str, event_id: str) -> str:
    """Build a stable, delimiter-safe identity for one session event."""

    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be a non-empty string")
    name = f"{SOURCE_NAME_PREFIX}:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(name) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("composed Pi event Source identity exceeds the 256-character limit")
    return name


def _string_or_text_blocks(value: object) -> str | None:
    """Extract visible text from a string or a list of text blocks."""

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts) if parts else None
    return None


def _message_body(content: object) -> str | None:
    """Extract the visible body of a regular message (text and tool calls)."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif block_type == "toolCall":
                name = _as_string(block.get("name"))
                call_id = _as_string(block.get("id"))
                arguments = block.get("arguments", {})
                parts.append(f"[tool_call {name} {call_id}] {_compact_json(arguments)}")
        return "\n".join(parts) if parts else None
    return None


def _visible_event(event_type: str, record: dict[str, JsonValue]) -> tuple[str, str] | None:
    """Return the ``(role, body)`` of a visible event, or ``None`` if hidden."""

    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary:
            return None
        return event_type, summary

    if event_type == "custom_message":
        body = _string_or_text_blocks(record.get("content"))
        if not body:
            return None
        return "custom", body

    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        if role == "toolResult":
            tool_name = _as_string(message.get("toolName")) or "unknown"
            call_id = _as_string(message.get("toolCallId"))
            text = _string_or_text_blocks(message.get("content")) or ""
            return role, f"[tool_result {tool_name} {call_id}] {text}"
        if role == "bashExecution":
            command = _as_string(message.get("command"))
            output = _as_string(message.get("output"))
            if not command and not output:
                return None
            return role, f"$ {command}\n{output}"
        body = _message_body(message.get("content"))
        if not body:
            return None
        return role, body

    # Control events (model_change, thinking_level_change, and any other
    # non-text event) keep their graph edges but produce no text evidence.
    return None


class _PiEventParseState:
    """Accumulate validated records while parsing a session atomically."""

    def __init__(self, header: dict[str, JsonValue]) -> None:
        self.session_id = _as_string(header.get("id"))
        self.records: list[dict[str, JsonValue]] = []
        self.by_id: dict[str, dict[str, JsonValue]] = {}

    def add(self, record: dict[str, JsonValue]) -> None:
        """Validate one record's graph edge and register it."""

        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("every Pi record must have a non-empty string id")
        if event_id in self.by_id:
            raise ValueError(f"duplicate Pi record id: {event_id!r}")
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or parent_id not in self.by_id:
                raise ValueError(f"Pi record {event_id!r} has an invalid parent")
        self.records.append(record)
        self.by_id[event_id] = record


def _decode_records(content: bytes) -> list[dict[str, JsonValue]]:
    """Decode and JSON-parse every non-blank JSONL line of a session."""

    if not isinstance(content, (bytes, bytearray)) or not content:
        raise ValueError("Pi session content must be non-empty bytes")
    try:
        text = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        logger.warning("Pi session content is not valid UTF-8: %s", error)
        raise ValueError("Pi session content is not valid UTF-8") from error

    records: list[dict[str, JsonValue]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            logger.warning("Pi session contains malformed JSON: %s", error)
            raise ValueError("Pi session contains malformed JSON") from error
        if not isinstance(parsed, dict):
            raise ValueError("every Pi session line must be a JSON object")
        records.append(parsed)
    if not records:
        raise ValueError("Pi session has no records")
    return records


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Parse and validate one Pi session into the selected branch's visible events.

    The parse is atomic: invalid encoding, malformed JSON, an invalid header,
    duplicate ids, missing parents, cycles or a missing leaf all raise
    ``ValueError`` without returning a partial result.  The returned events
    cover the complete root-to-leaf ancestor chain of the selected node, in
    order, with graph edges preserved through ``parent_id`` while control
    nodes produce no text evidence.
    """

    records = _decode_records(value.content)
    header = records[0]
    if header.get("type") != "session" or header.get("version") != 3:
        raise ValueError("first Pi record must be a version 3 session header")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Pi session header id must be a non-empty string")

    state = _PiEventParseState(header)
    for record in records[1:]:
        state.add(record)

    leaf_id = value.leaf_id
    if leaf_id is None:
        if not state.records:
            raise ValueError("Pi session has no selectable leaf")
        leaf_id = state.records[-1]["id"]
    if not isinstance(leaf_id, str) or leaf_id not in state.by_id:
        raise ValueError(f"selected Pi leaf is absent: {leaf_id!r}")

    # Walk ancestors from the leaf to the root; a cycle cannot occur because
    # each parent had to reference an already-seen node during registration.
    chain: list[dict[str, JsonValue]] = []
    seen_chain: set[str] = set()
    current_id: str | None = leaf_id
    while current_id is not None:
        if current_id in seen_chain:
            raise ValueError("cycle detected in the selected Pi branch")
        seen_chain.add(current_id)
        record = state.by_id[current_id]
        chain.append(record)
        parent = record.get("parentId")
        current_id = parent if isinstance(parent, str) else None
    chain.reverse()

    events: list[PiEventInput] = []
    for record in chain:
        event_type = record.get("type")
        if not isinstance(event_type, str):
            continue
        visible = _visible_event(event_type, record)
        if visible is None:
            continue
        role, body = visible
        event_id = _as_string(record.get("id"))
        parent = record.get("parentId")
        parent_id = parent if isinstance(parent, str) else None
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=event_id,
                parent_id=parent_id,
                event_type=event_type,
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        )
    return tuple(events)


class _PiEventSourceAdapter:
    """Resolve a visible event into a durable Source and read it back."""

    name = SOURCE_TYPE
    input_class = PiEventInput
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        identity = pi_event_source_name(value.session_id, value.event_id)
        return PiEventSource(
            name=identity,
            definition_version=DEFINITION_VERSION,
            materialization=SourceMaterialization.CAPTURED,
            source_id=identity,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class _PiEventTextEvidenceProjection:
    """Standard text-evidence projection carrying the full raw event text."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=SOURCE_TYPE,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    adapter=_PiEventSourceAdapter(),
    version=DEFINITION_VERSION,
    projections=(_PiEventTextEvidenceProjection(),),
)


def _checkpoint_entries(checkpoint: Any) -> dict[str, str]:
    """Normalize a persisted checkpoint into an identity-to-digest mapping."""

    if not isinstance(checkpoint, dict):
        return {}
    entries = checkpoint.get(CHECKPOINT_EVENTS_KEY)
    if not isinstance(entries, dict):
        return {}
    return {key: value for key, value in entries.items() if isinstance(key, str) and isinstance(value, str)}


class PiSessionConnector:
    """Incrementally import the selected branch of one Pi session file."""

    name = CONNECTOR_NAME
    version = DEFINITION_VERSION
    source_definitions = frozenset({SOURCE_TYPE})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        """Parse the current file and submit only unseen visible events.

        All already-imported events on the branch are verified against their
        stored record digests before anything new is submitted, so a history
        rewrite raises ``ValueError`` without partially advancing the run.  A
        new checkpoint is committed by the lifecycle only after every
        submission succeeded.
        """

        raw = self._path.read_bytes()
        events = parse_pi_session(PiSessionInput(content=raw, leaf_id=self._leaf_id))

        previous = session.checkpoint
        imported = _checkpoint_entries(previous)
        pending: list[tuple[PiEventInput, str, str]] = []
        for event in events:
            identity = pi_event_source_name(event.session_id, event.event_id)
            digest = _record_digest(event.record)
            known = imported.get(identity)
            if known is not None:
                if known != digest:
                    raise ValueError(f"imported Pi event was rewritten: {identity}")
                continue
            pending.append((event, identity, digest))

        for event, identity, _digest in pending:
            await session.submit(identity, SOURCE_TYPE, event)

        if not pending:
            return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=previous)

        updated = dict(imported)
        for _event, identity, digest in pending:
            updated[identity] = digest
        checkpoint: dict[str, JsonValue] = {
            CHECKPOINT_VERSION_KEY: CHECKPOINT_VERSION,
            CHECKPOINT_EVENTS_KEY: dict(sorted(updated.items())),
        }
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=checkpoint)


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_name",
]

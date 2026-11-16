# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Source-withdrawal lifecycle for Memory and Topic Memory dependency graphs.

A scope owns one DAG whose leaves are captured Sources and whose derived nodes are
Memory entries or Topic Memory artifacts.  A lifecycle event flips one Source's
availability; the change propagates through ``all`` / ``any`` policies to every
descendant.  Native Memory entries are deactivated / reactivated so that search
and Context follow the result, while Topic Memory results are filtered by this
module's persisted active set.  All graph state is stored in the Scope's database
so a restart observes the same availability and a successful event replay is a
no-op.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory.errors import (
    CapabilityNotSupportedError,
    InvalidMemoryCitationError,
    MemoryEntryNotFoundError,
)
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.errors import DatabaseClosedError
from powercontext.builtin.persistence.tables import identity_string
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import ArtifactError, SourceError
from powercontext.limits import MAX_SCOPE_ID_LENGTH
from powercontext.sources.models import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

logger = logging.getLogger(__name__)

LIFECYCLE_TABLE_NAME: Final = "pc_source_lifecycle_nodes"
FAMILY_SOURCE: Final = "source"
FAMILY_MEMORY: Final = "memory"
FAMILY_TOPIC: Final = "topic"
POLICY_ALL: Final = "all"
POLICY_ANY: Final = "any"
PURPOSE_GENERATION: Final = "generation"
PURPOSE_AUDIT: Final = "audit"
VALID_POLICIES: Final = frozenset({POLICY_ALL, POLICY_ANY})
VALID_PURPOSES: Final = frozenset({PURPOSE_GENERATION, PURPOSE_AUDIT})

_REFERENCE_TYPES: Final[dict[str, type[BaseModel]]] = {
    "memory_citation": MemoryCitation,
    "artifact_ref": ArtifactRef,
    "source_ref": SourceRef,
}
_READ_ERRORS: Final = (SourceError, ArtifactError, MemoryEntryNotFoundError, InvalidMemoryCitationError)

_NODES_TABLE = Table(
    LIFECYCLE_TABLE_NAME,
    MetaData(),
    Column("scope_id", identity_string(MAX_SCOPE_ID_LENGTH), primary_key=True),
    Column("node_id", identity_string(256), primary_key=True),
    Column("family", String(16), nullable=False),
    Column("policy", String(8), nullable=False),
    Column("registration_index", Integer, nullable=False),
    Column("parents_json", Text, nullable=False),
    Column("reference_kind", String(32), nullable=False),
    Column("reference_json", Text, nullable=False),
    Column("revision", Integer, nullable=False, default=0),
    Column("sequence", Integer, nullable=False, default=0),
    Column("event_json", Text, nullable=False, default=""),
    Column("active", Boolean, nullable=False, default=True),
)


class SourceLifecycleError(ValueError):
    """A lifecycle registration, event, or resolution violated its contract."""


class SourceLifecycleEvent(BaseModel):
    """A validated Source availability event applied by ``apply``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    reason: str
    revision: StrictInt = Field(gt=0)
    sequence: StrictInt = Field(gt=0)
    available: StrictBool

    @field_validator("node_id", "reason")
    @classmethod
    def _require_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")  # noqa: TRY003
        return value


@dataclass(frozen=True, slots=True)
class _Node:
    """One persisted lifecycle node."""

    node_id: str
    family: str
    policy: str
    registration_index: int
    parents: tuple[str, ...]
    reference_kind: str
    reference_json: str
    revision: int
    sequence: int
    event_json: str
    active: bool

    @property
    def reference(self) -> BaseModel:
        return _decode_reference(self.reference_kind, self.reference_json)


def _database_of(runtime: BuiltinRuntime) -> AsyncDatabase:
    provider = getattr(runtime, "_provider", None)
    database = getattr(provider, "database", None)
    if not isinstance(database, AsyncDatabase):
        raise SourceLifecycleError("lifecycle-storage-unavailable")
    return database


def _reference_kind(reference: BaseModel) -> str:
    if isinstance(reference, MemoryCitation):
        return "memory_citation"
    if isinstance(reference, ArtifactRef):
        return "artifact_ref"
    if isinstance(reference, SourceRef):
        return "source_ref"
    raise SourceLifecycleError("unsupported-reference")


def _decode_reference(kind: str, payload: str) -> BaseModel:
    reference_type = _REFERENCE_TYPES.get(kind)
    if reference_type is None:
        raise SourceLifecycleError("unknown-reference-kind")
    try:
        return reference_type.model_validate_json(payload)
    except (ValueError, TypeError) as error:
        logger.error("Failed to decode lifecycle reference %s", kind, exc_info=error)
        raise SourceLifecycleError("invalid-reference-payload") from error


def _node_from_row(row: dict[str, Any]) -> _Node:
    return _Node(
        node_id=str(row["node_id"]),
        family=str(row["family"]),
        policy=str(row["policy"]),
        registration_index=int(row["registration_index"]),
        parents=tuple(json.loads(str(row["parents_json"]))),
        reference_kind=str(row["reference_kind"]),
        reference_json=str(row["reference_json"]),
        revision=int(row["revision"]),
        sequence=int(row["sequence"]),
        event_json=str(row["event_json"]),
        active=bool(row["active"]),
    )


async def _ensure_table(connection: AsyncConnection) -> None:
    await connection.run_sync(_NODES_TABLE.create, checkfirst=True)


async def _load_nodes(database: AsyncDatabase, scope_id: str) -> tuple[_Node, ...]:
    async with database.transaction() as connection:
        await _ensure_table(connection)
        rows = (
            await connection.execute(
                select(_NODES_TABLE)
                .where(_NODES_TABLE.c.scope_id == scope_id)
                .order_by(_NODES_TABLE.c.registration_index)
            )
        ).mappings()
        return tuple(_node_from_row(dict(row)) for row in rows)


async def _insert_node(database: AsyncDatabase, scope_id: str, node: _Node) -> None:
    async with database.transaction() as connection:
        await _ensure_table(connection)
        await connection.execute(
            insert(_NODES_TABLE).values(
                scope_id=scope_id,
                node_id=node.node_id,
                family=node.family,
                policy=node.policy,
                registration_index=node.registration_index,
                parents_json=json.dumps(list(node.parents)),
                reference_kind=node.reference_kind,
                reference_json=node.reference_json,
                revision=node.revision,
                sequence=node.sequence,
                event_json=node.event_json,
                active=node.active,
            )
        )


async def _persist_states(
    database: AsyncDatabase,
    scope_id: str,
    states: dict[str, bool],
    source_node_id: str,
    source_sequence: int,
    source_event_json: str,
) -> None:
    async with database.transaction() as connection:
        await _ensure_table(connection)
        for node_id, active in states.items():
            await connection.execute(
                update(_NODES_TABLE)
                .where(_NODES_TABLE.c.scope_id == scope_id, _NODES_TABLE.c.node_id == node_id)
                .values(active=active)
            )
        await connection.execute(
            update(_NODES_TABLE)
            .where(_NODES_TABLE.c.scope_id == scope_id, _NODES_TABLE.c.node_id == source_node_id)
            .values(sequence=source_sequence, event_json=source_event_json)
        )


def _compute_states(nodes: tuple[_Node, ...], override_node: str, override_active: bool) -> dict[str, bool]:
    states = {node.node_id: node.active for node in nodes}
    states[override_node] = override_active
    for node in nodes:
        if node.family == FAMILY_SOURCE:
            continue
        support = [states.get(parent, False) for parent in node.parents]
        states[node.node_id] = all(support) if node.policy == POLICY_ALL else any(support)
    return states


def _sorted_active(states: dict[str, bool]) -> list[str]:
    return sorted(node_id for node_id, active in states.items() if active)


class ScopedSourceLifecycle:
    """Register a scope's dependency graph, apply events, and resolve references."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)
        self._database = _database_of(runtime)

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        self._validate_identity(node_id)
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("invalid-revision")
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source-reference-type")
        node = _Node(
            node_id=node_id,
            family=FAMILY_SOURCE,
            policy="",
            registration_index=0,
            parents=(),
            reference_kind=_reference_kind(source_ref),
            reference_json=source_ref.model_dump_json(),
            revision=revision,
            sequence=0,
            event_json="",
            active=True,
        )
        await self._register(node)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory-reference-type")
        await self._register_derived(node_id, citation, FAMILY_MEMORY, parents, policy)

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        *,
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        if not isinstance(reference, ArtifactRef):
            raise SourceLifecycleError("topic-reference-type")
        await self._register_derived(node_id, reference, FAMILY_TOPIC, parents, policy)

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event-type")
        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await _load_nodes(self._database, self.scope_id)
            source = next((node for node in nodes if node.node_id == event.node_id), None)
            if source is None or source.family != FAMILY_SOURCE:
                raise SourceLifecycleError("unknown-source")
            if event.sequence < source.sequence:
                raise SourceLifecycleError("sequence-regression")
            if event.sequence == source.sequence:
                if source.event_json == event.model_dump_json():
                    return _sorted_active({node.node_id: node.active for node in nodes})
                raise SourceLifecycleError("sequence-conflict")
            if event.revision != source.revision:
                raise SourceLifecycleError("revision-mismatch")
            states = _compute_states(nodes, event.node_id, event.available)
            await self._propagate_memory(nodes, states, event.reason)
            await _persist_states(
                self._database,
                self.scope_id,
                states,
                event.node_id,
                event.sequence,
                event.model_dump_json(),
            )
            return _sorted_active(states)

    async def resolve(self, node_id: str, *, purpose: str) -> BaseModel:
        if purpose not in VALID_PURPOSES:
            raise SourceLifecycleError("unknown-purpose")
        nodes = await _load_nodes(self._database, self.scope_id)
        node = next((item for item in nodes if item.node_id == node_id), None)
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == PURPOSE_GENERATION and not node.active:
            raise SourceLifecycleError("inactive-node")
        return node.reference

    async def _register(self, node: _Node) -> None:
        self._validate_identity(node.node_id)
        nodes = await _load_nodes(self._database, self.scope_id)
        existing = next((item for item in nodes if item.node_id == node.node_id), None)
        if existing is not None:
            if self._same_binding(existing, node):
                return
            raise SourceLifecycleError("conflicting-registration")
        active = await self._validate_reference(node)
        registration_index = 1 + max((item.registration_index for item in nodes), default=-1)
        stored = _Node(
            node_id=node.node_id,
            family=node.family,
            policy=node.policy,
            registration_index=registration_index,
            parents=node.parents,
            reference_kind=node.reference_kind,
            reference_json=node.reference_json,
            revision=node.revision,
            sequence=node.sequence,
            event_json=node.event_json,
            active=active,
        )
        await _insert_node(self._database, self.scope_id, stored)

    async def _register_derived(
        self,
        node_id: str,
        reference: BaseModel,
        family: str,
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        self._validate_identity(node_id)
        if family not in {FAMILY_MEMORY, FAMILY_TOPIC}:
            raise SourceLifecycleError("unsupported-family")
        if policy not in VALID_POLICIES:
            raise SourceLifecycleError("invalid-policy")
        if not parents or len(set(parents)) != len(parents):
            raise SourceLifecycleError("invalid-parents")
        if node_id in parents:
            raise SourceLifecycleError("self-cycle")
        await self._register(
            _Node(
                node_id=node_id,
                family=family,
                policy=policy,
                registration_index=0,
                parents=parents,
                reference_kind=_reference_kind(reference),
                reference_json=reference.model_dump_json(),
                revision=0,
                sequence=0,
                event_json="",
                active=True,
            )
        )

    @staticmethod
    def _validate_identity(node_id: str) -> None:
        if not isinstance(node_id, str) or not node_id.strip():
            raise SourceLifecycleError("invalid-node-id")

    @staticmethod
    def _same_binding(existing: _Node, candidate: _Node) -> bool:
        return (
            existing.family == candidate.family
            and existing.policy == candidate.policy
            and existing.parents == candidate.parents
            and existing.reference_kind == candidate.reference_kind
            and existing.reference_json == candidate.reference_json
            and existing.revision == candidate.revision
        )

    async def _validate_reference(self, node: _Node) -> bool:
        reference = node.reference
        try:
            if node.family == FAMILY_SOURCE:
                source_ref = cast("SourceRef", reference)
                await self._runtime.records.for_scope(self.scope_id).get_source(
                    source_ref.source_type, source_ref.source_id
                )
            elif node.family == FAMILY_MEMORY:
                memory_citation = cast("MemoryCitation", reference)
                await self._runtime.memory.for_scope(self.scope_id).get(
                    GetMemoryEntryRequest(citation=memory_citation)
                )
            else:
                artifact_ref = cast("ArtifactRef", reference)
                await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                    artifact_ref.family, artifact_ref.artifact_id, artifact_ref.revision
                )
        except _READ_ERRORS as error:
            logger.error("Lifecycle reference %s is unreadable", node.node_id, exc_info=error)
            raise SourceLifecycleError("unreadable-reference") from error
        return await self._validate_parents(node)

    async def _validate_parents(self, node: _Node) -> bool:
        if node.family == FAMILY_SOURCE:
            return True
        nodes = await _load_nodes(self._database, self.scope_id)
        known = {item.node_id for item in nodes}
        if any(parent not in known for parent in node.parents):
            raise SourceLifecycleError("missing-parent")
        by_id = {item.node_id: item for item in nodes}
        support = [by_id[parent].active for parent in node.parents]
        node_active = all(support) if node.policy == POLICY_ALL else any(support)
        if node.family == FAMILY_MEMORY and not node_active:
            await self._set_memory_state(cast("MemoryCitation", node.reference), False, "lifecycle registration")
        return node_active

    async def _propagate_memory(
        self,
        nodes: tuple[_Node, ...],
        states: dict[str, bool],
        reason: str,
    ) -> None:
        to_deactivate = [
            node for node in nodes
            if node.family == FAMILY_MEMORY and node.active and not states[node.node_id]
        ]
        to_reactivate = [
            node for node in nodes
            if node.family == FAMILY_MEMORY and not node.active and states[node.node_id]
        ]
        if not to_deactivate and not to_reactivate:
            return
        deactivate_entries = await self._memory_entries(to_deactivate)
        reactivate_entries = await self._memory_entries(to_reactivate)
        if not deactivate_entries and not reactivate_entries:
            return
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            current = await service.head(context.artifacts.memory_artifact_id)
            if deactivate_entries:
                current = await service.forget(current, entries=deactivate_entries, reason=reason)
            if reactivate_entries:
                current = await service.reactivate(current, entries=reactivate_entries, reason=reason)
            logger.info(
                "Lifecycle propagated %d deactivations and %d reactivations in %s",
                len(deactivate_entries),
                len(reactivate_entries),
                self.scope_id,
            )

    async def _memory_entries(self, nodes: list[_Node]) -> tuple[Any, ...]:
        if not nodes:
            return ()
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            entries = []
            for node in nodes:
                try:
                    entries.append(await service.validate_citation(cast("MemoryCitation", node.reference)))
                except (InvalidMemoryCitationError, MemoryEntryNotFoundError, CapabilityNotSupportedError) as error:
                    logger.error("Lifecycle citation %s is unreadable", node.node_id, exc_info=error)
                    raise SourceLifecycleError("unreadable-citation") from error
            return tuple(entries)

    async def _set_memory_state(self, citation: MemoryCitation, active: bool, reason: str) -> None:
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            current = await service.head(context.artifacts.memory_artifact_id)
            try:
                entry = await service.validate_citation(citation)
                updated = (
                    await service.reactivate(current, entries=(entry,), reason=reason)
                    if active
                    else await service.forget(current, entries=(entry,), reason=reason)
                )
            except (InvalidMemoryCitationError, MemoryEntryNotFoundError, CapabilityNotSupportedError) as error:
                logger.error("Lifecycle cannot change memory state", exc_info=error)
                raise SourceLifecycleError("memory-state-write-failed") from error
            logger.info("Lifecycle set memory state for %s to %s", citation.entry_id, updated.as_ref())


class SourceLifecycleApplication:
    """Select the lifecycle service for one scope."""

    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, scope_id)


async def inactive_topic_artifact_ids(runtime: BuiltinRuntime, scope_id: str) -> frozenset[str]:
    """Return artifact ids of lifecycle-managed Topics currently withdrawn."""

    service = ScopedSourceLifecycle(runtime, scope_id)
    nodes = await _load_nodes(service._database, service.scope_id)
    return frozenset(
        cast("ArtifactRef", node.reference).artifact_id
        for node in nodes
        if node.family == FAMILY_TOPIC and not node.active and isinstance(node.reference, ArtifactRef)
    )


async def filter_inactive_topic_hits(runtime: BuiltinRuntime, scope_id: str, result: Any) -> Any:
    """Filter inactive lifecycle Topics out of a completed Topic search result."""

    try:
        inactive = await inactive_topic_artifact_ids(runtime, scope_id)
    except (SourceLifecycleError, DatabaseClosedError, SQLAlchemyError) as error:
        logger.warning("Lifecycle topic filter unavailable for %s", scope_id, exc_info=error)
        return result
    if not inactive:
        return result
    hits = tuple(hit for hit in result.hits if hit.artifact_ref.artifact_id not in inactive)
    if len(hits) == len(result.hits):
        return result
    return result.model_copy(update={"hits": hits})


__all__ = [
    "ScopedSourceLifecycle",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "filter_inactive_topic_hits",
]

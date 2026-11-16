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

"""Context Pack wire protocol and incremental delta reconstruction.

A :class:`ContextPack` is the complete, chunked representation of one prepared
context.  A :class:`ContextPackDelta` reuses a prefix of a previously delivered
pack and carries only the remaining suffix, so a receiver holding the base pack
can rebuild the exact target pack without any sender-side state.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from powercontext._logging import log_safely
from powercontext.errors import PowerContextError

logger = logging.getLogger(__name__)

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DIGEST_PREFIX = "sha256:"
DEFAULT_CHUNK_BYTES = 512

_HEX_DIGEST_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")
_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_MAX_BYTE_COUNT_ROUNDS = 16


class ContextPackError(PowerContextError, ValueError):
    """Raised when a Context Pack or delta fails protocol validation."""


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _normalize_items(items: object) -> tuple[bytes, ...]:
    if not isinstance(items, (tuple, list)):
        raise ContextPackError("items must be a sequence of bytes")
    if any(not isinstance(item, bytes) for item in items):
        raise ContextPackError("every item must be bytes")
    return tuple(items)


def _decode_items(values: object, field: str) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError(f"{field} must be an array of base64 strings")
    decoded: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError(f"{field} entries must be strings")
        try:
            decoded.append(base64.b64decode(value, validate=True))
        except (binascii.Error, ValueError) as error:
            log_safely(logger, logging.WARNING, "Invalid base64 in context pack wire", exc_info=error)
            raise ContextPackError(f"invalid base64 in {field}") from error
    return tuple(decoded)


def _is_digest(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith(DIGEST_PREFIX):
        return False
    hex_part = value[len(DIGEST_PREFIX) :]
    return len(hex_part) == _HEX_DIGEST_LENGTH and all(char in _HEX_DIGITS for char in hex_part)


def _digest_for(schema: str, items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": schema, "items": _encode_items(items)}
    return DIGEST_PREFIX + hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()


def _parse_wire(data: bytes, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)):
        raise ContextPackError(f"{label} wire must be bytes")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as error:
        log_safely(logger, logging.WARNING, "Context pack wire is not UTF-8", exc_info=error)
        raise ContextPackError(f"{label} wire is not UTF-8") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        log_safely(logger, logging.WARNING, "Context pack wire is not valid JSON", exc_info=error)
        raise ContextPackError(f"{label} wire is not valid JSON") from error
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ContextPackError(f"{label} wire has missing or extra fields")
    return payload


def _pack_byte_count(items: tuple[bytes, ...], digest: str) -> int:
    """Resolve the self-referential ``byte_count`` fixed point."""
    unsigned = {"schema": PACK_SCHEMA, "items": _encode_items(items), "digest": digest}
    candidate = 0
    for _ in range(_MAX_BYTE_COUNT_ROUNDS):
        wire = _canonical_bytes({**unsigned, "byte_count": candidate})
        if len(wire) == candidate:
            return candidate
        candidate = len(wire)
    raise ContextPackError("context pack byte_count did not converge")


def _chunk_bytes(raw: bytes, limit: int) -> tuple[bytes, ...]:
    """Split UTF-8 bytes greedily on code-point boundaries, bounded by ``limit``."""
    chunks: list[bytes] = []
    current = bytearray()
    for char in raw.decode("utf-8"):
        encoded = char.encode("utf-8")
        if len(encoded) > limit:
            raise ContextPackError("chunk limit cannot hold one UTF-8 character")
        if current and len(current) + len(encoded) > limit:
            chunks.append(bytes(current))
            current.clear()
        current.extend(encoded)
    if current:
        chunks.append(bytes(current))
    return tuple(chunks)


def _common_prefix_len(base: tuple[bytes, ...], target: tuple[bytes, ...]) -> int:
    limit = min(len(base), len(target))
    prefix = 0
    while prefix < limit and base[prefix] == target[prefix]:
        prefix += 1
    return prefix


@dataclass(frozen=True, slots=True)
class ContextPack:
    """Complete, chunked context with its digest and exact wire byte count."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        normalized = _normalize_items(items)
        digest = _digest_for(PACK_SCHEMA, normalized)
        return cls(normalized, digest, _pack_byte_count(normalized, digest))

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        if chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")
        if not content:
            return cls.build(())
        return cls.build(_chunk_bytes(content.encode("utf-8"), chunk_bytes))

    def to_bytes(self) -> bytes:
        payload = {
            "schema": PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
            "byte_count": self.byte_count,
        }
        return _canonical_bytes(payload)

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        payload = _parse_wire(data, _PACK_FIELDS, "context pack")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("unexpected context pack schema")
        digest = payload["digest"]
        if not _is_digest(digest):
            raise ContextPackError("invalid context pack digest")
        items = _decode_items(payload["items"], "items")
        if digest != _digest_for(PACK_SCHEMA, items):
            raise ContextPackError("context pack digest mismatch")
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count < 0:
            raise ContextPackError("invalid context pack byte_count")
        candidate = cls.build(items)
        if candidate.byte_count != byte_count:
            raise ContextPackError("context pack byte_count mismatch")
        return candidate


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """Reusable prefix plus appended suffix that rebuilds one target pack."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        prefix = _common_prefix_len(base.items, target.items)
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        payload = {
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        }
        return _canonical_bytes(payload)

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        payload = _parse_wire(data, _DELTA_FIELDS, "context pack delta")
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("unexpected context pack delta schema")
        base_digest = payload["base_digest"]
        target_digest = payload["target_digest"]
        if not _is_digest(base_digest) or not _is_digest(target_digest):
            raise ContextPackError("invalid context pack delta digest")
        prefix = payload["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        appended = _decode_items(payload["appended"], "appended")
        return cls(base_digest, target_digest, prefix, appended)


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Validate ``delta`` against ``base`` and return the reconstructed target pack."""
    if not _is_digest(delta.base_digest) or not _is_digest(delta.target_digest):
        raise ContextPackError("invalid digest in context pack delta")
    if delta.base_digest != base.digest:
        raise ContextPackError("delta base digest does not match base pack")
    prefix = delta.prefix_items
    if type(prefix) is not int or prefix < 0 or prefix > len(base.items):
        raise ContextPackError("delta prefix_items is out of range")
    appended = _normalize_items(delta.appended)
    target = ContextPack.build(base.items[:prefix] + appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest mismatch")
    return target


__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]

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

"""Cross-call request coalescing and bounded vector caching for embeddings.

``CoalescingEmbeddingModel`` wraps any :class:`EmbeddingModel` and deduplicates
texts across concurrent ``embed`` calls.  A single background dispatcher
collects texts inside a short window, splits them into bounded batches and
runs each batch in its own task so that cancelling one caller never cancels
work shared with other callers.  Successful vectors are kept in an LRU cache.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable

from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

_LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_BATCH_SIZE = 64
DEFAULT_COALESCE_MS = 5.0
DEFAULT_CACHE_SIZE = 1024
_MILLISECONDS_PER_SECOND = 1000.0


@dataclass(slots=True)
class _BatchOutcome:
    """Successful vectors for one batch, keyed by text, plus its provider usage."""

    vectors: dict[str, EmbeddingVector]
    usage: InferenceUsage


@dataclass(slots=True)
class _Batch:
    """One dispatched provider request shared by all interested callers."""

    id: int
    texts: tuple[str, ...] = ()
    outcome: _BatchOutcome | None = None
    error: BaseException | None = None
    task: asyncio.Task[None] | None = None
    observers: list[_Call] = field(default_factory=list)

    def add_observer(self, call: _Call) -> None:
        """Register a caller to be notified when this batch settles."""
        self.observers.append(call)


@dataclass(slots=True, eq=False)
class _Call:
    """Per-``embed`` state: resolved vectors, outstanding texts and usage."""

    pending_texts: set[str] = field(default_factory=set)
    vectors: dict[str, EmbeddingVector] = field(default_factory=dict)
    usages: dict[int, InferenceUsage] = field(default_factory=dict)
    watched_batches: set[int] = field(default_factory=set)
    error: BaseException | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def watch(self, batch: _Batch) -> None:
        """Start waiting on ``batch`` once, regardless of how many texts share it."""
        if batch.id in self.watched_batches:
            return
        self.watched_batches.add(batch.id)
        batch.add_observer(self)

    def resolve_from_cache(self, text: str, vector: EmbeddingVector) -> None:
        """Fill a vector already available from the cache."""
        self.vectors[text] = vector
        self.pending_texts.discard(text)
        self._signal_if_settled()

    def on_batch_done(self, batch: _Batch) -> None:
        """Absorb the outcome of a batch this call was waiting on."""
        if batch.id not in self.watched_batches:
            return
        if batch.error is not None:
            if self.error is None:
                self.error = batch.error
            self.done.set()
            return
        outcome = batch.outcome
        if outcome is not None:
            self.usages[batch.id] = outcome.usage
            for text, vector in outcome.vectors.items():
                if text in self.pending_texts:
                    self.vectors[text] = vector
                    self.pending_texts.discard(text)
        self._signal_if_settled()

    def _signal_if_settled(self) -> None:
        if not self.pending_texts:
            self.done.set()


def _merge_usage(usages: Iterable[InferenceUsage]) -> InferenceUsage:
    """Sum provider usage across the distinct batches a call waited on."""
    requests = 0
    input_tokens = 0
    output_tokens = 0
    for usage in usages:
        requests += usage.requests
        input_tokens += usage.input_tokens or 0
        output_tokens += usage.output_tokens or 0
    return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)


class CoalescingEmbeddingModel:
    """Coalesce concurrent embedding requests and cache successful vectors."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self._model: EmbeddingModel = model
        self.profile: EmbeddingProfile = model.profile
        self._max_batch_size = max(1, max_batch_size)
        self._coalesce_seconds = max(0.0, coalesce_ms) / _MILLISECONDS_PER_SECOND
        self._cache_size = max(0, cache_size)

        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._pending: OrderedDict[str, None] = OrderedDict()
        self._waiters: dict[str, set[_Call]] = {}
        self._deferred_waiters: dict[str, set[_Call]] = {}
        self._inflight: dict[str, _Batch] = {}
        self._wake: asyncio.Event = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None
        self._next_id = 0

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one vector per input text, in input order with duplicates kept."""
        text_tuple = tuple(texts)
        if not text_tuple:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0, input_tokens=0, output_tokens=0))

        call = _Call(pending_texts=set(text_tuple))
        dispatch_needed = False
        for text in dict.fromkeys(text_tuple):
            if text in self._cache:
                self._cache.move_to_end(text)
                call.resolve_from_cache(text, self._cache[text])
            elif text in self._inflight:
                call.watch(self._inflight[text])
            else:
                if text not in self._pending:
                    self._pending[text] = None
                    dispatch_needed = True
                self._waiters.setdefault(text, set()).add(call)

        if dispatch_needed:
            if self._dispatcher is None:
                self._dispatcher = asyncio.create_task(self._run_dispatcher())
            self._wake.set()

        await call.done.wait()
        if call.error is not None:
            raise call.error

        vectors = tuple(call.vectors[text] for text in text_tuple)
        return EmbeddingResult(vectors=vectors, usage=_merge_usage(call.usages.values()))

    async def _run_dispatcher(self) -> None:
        """Poll for pending work and flush it in coalesced, bounded batches."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            await asyncio.sleep(self._coalesce_seconds)
            self._drain()

    def _drain(self) -> None:
        """Turn all currently pending texts into dispatched batches."""
        if not self._pending:
            return
        texts = tuple(self._pending)
        self._pending.clear()
        to_batch: list[str] = []
        for text in texts:
            waiters = self._waiters.pop(text, set())
            if text in self._cache:
                self._cache.move_to_end(text)
                for call in waiters:
                    call.resolve_from_cache(text, self._cache[text])
            elif text in self._inflight:
                for call in waiters:
                    call.watch(self._inflight[text])
            else:
                to_batch.append(text)
                self._deferred_waiters[text] = waiters

        for start in range(0, len(to_batch), self._max_batch_size):
            chunk = tuple(to_batch[start : start + self._max_batch_size])
            batch = self._create_batch(chunk)
            for text in chunk:
                for call in self._deferred_waiters.pop(text, ()):
                    call.watch(batch)

    def _create_batch(self, texts: tuple[str, ...]) -> _Batch:
        """Create an independent provider task and mark its texts in flight."""
        batch = _Batch(id=self._next_id, texts=texts)
        self._next_id += 1
        batch.task = asyncio.create_task(self._run_batch(batch, texts))
        batch.task.add_done_callback(lambda _task: self._on_batch_done(batch))
        for text in texts:
            self._inflight[text] = batch
        return batch

    async def _run_batch(self, batch: _Batch, texts: tuple[str, ...]) -> None:
        """Call the provider once for ``texts`` and validate the vector count."""
        try:
            result = await self._model.embed(texts)
        except Exception as exc:
            _LOGGER.warning("embedding batch %d failed: %s", batch.id, exc)
            batch.error = exc
            return
        vectors = result.vectors
        if len(vectors) != len(texts):
            batch.error = ValueError(
                f"embedding returned {len(vectors)} vectors for {len(texts)} texts"
            )
            return
        batch.outcome = _BatchOutcome(
            vectors=dict(zip(texts, vectors, strict=True)),
            usage=result.usage,
        )

    def _on_batch_done(self, batch: _Batch) -> None:
        """Cache successful vectors, clear in-flight markers, notify callers."""
        succeeded = batch.error is None and batch.outcome is not None
        if succeeded and batch.outcome is not None:
            for text, vector in batch.outcome.vectors.items():
                if self._cache_size > 0:
                    self._cache[text] = vector
                    self._cache.move_to_end(text)
        for text in batch.texts:
            self._inflight.pop(text, None)
        self._trim_cache()
        for call in tuple(batch.observers):
            call.on_batch_done(batch)

    def _trim_cache(self) -> None:
        """Evict least-recently-used vectors beyond the configured capacity."""
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


__all__ = ["CoalescingEmbeddingModel"]

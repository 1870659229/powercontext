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

"""HTTP scoring adapter for identity-preserving, cross-batch Memory reranking."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import NoReturn

import httpx

from powercontext._logging import log_safely
from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

_DEFAULT_BATCH_SIZE = 32
_DEFAULT_POLICY_ID = "http-cross-encoder-v1"
_RESPONSE_KEY = "scores"
_SCORE_RANK_KEY = "rank"
_SCORE_VALUE_KEY = "score"

logger = logging.getLogger(__name__)


def _reject(reason: str) -> NoReturn:
    """Log and raise one ValueError for an invalid scoring response."""
    log_safely(
        logger,
        logging.WARNING,
        "HTTP rerank response rejected",
        extra={"reason": reason},
    )
    raise ValueError(f"Invalid HTTP rerank response: {reason}")  # noqa: TRY003


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_batch_response(
    payload: object,
    expected_ranks: frozenset[int],
) -> dict[int, float]:
    """Return one batch's rank-to-score map after strict schema and coverage checks."""
    if not isinstance(payload, dict):
        _reject("response body must be an object")
    if set(payload) != {_RESPONSE_KEY}:
        _reject("response body must contain exactly 'scores'")
    records = payload[_RESPONSE_KEY]
    if not isinstance(records, list):
        _reject("'scores' must be an array")

    scores: dict[int, float] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {_SCORE_RANK_KEY, _SCORE_VALUE_KEY}:
            _reject("each score record must contain exactly 'rank' and 'score'")
        rank = record[_SCORE_RANK_KEY]
        score = record[_SCORE_VALUE_KEY]
        if not _is_integer(rank):
            _reject("score rank must be an integer")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
            _reject("score must be a finite number")
        if rank not in expected_ranks:
            _reject("score rank does not belong to this batch")
        if rank in scores:
            _reject("duplicate score rank in one batch")
        scores[rank] = float(score)

    if frozenset(scores) != expected_ranks:
        _reject("scores do not cover every rank in the batch exactly once")
    return scores


def _validate_bounds(candidate_count: int, limit: int) -> None:
    """Reject an empty pool or a limit outside the candidate range."""
    if candidate_count < 1:
        raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
    if limit < 1 or limit > candidate_count:
        raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003


def _select_top_ranks(scores: dict[int, float], limit: int) -> tuple[int, ...]:
    """Select globally by descending score, then by ascending original rank."""
    ordered = sorted(scores, key=lambda rank: (-scores[rank], rank))
    return tuple(ordered[:limit])


class HTTPMemoryReranker:
    """Score candidate batches through an HTTP service and select globally by score."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        policy_id: str = _DEFAULT_POLICY_ID,
    ) -> None:
        if not _is_integer(batch_size) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate once and return the globally best original ranks."""
        _validate_bounds(len(candidates), limit)
        items = tuple((rank, candidate.text) for rank, candidate in enumerate(candidates, start=1))
        batches = tuple(
            items[start : start + self._batch_size]
            for start in range(0, len(items), self._batch_size)
        )
        responses = await asyncio.gather(
            *(
                self._request_scores(query, batch, frozenset(rank for rank, _ in batch))
                for batch in batches
            )
        )
        scores: dict[int, float] = {}
        for response in responses:
            scores.update(response)
        return MemoryRerankDecision(
            selected_ranks=_select_top_ranks(scores, limit),
            usage=InferenceUsage(requests=len(batches)),
        )

    async def _request_scores(
        self,
        query: str,
        batch: tuple[tuple[int, str], ...],
        expected_ranks: frozenset[int],
    ) -> dict[int, float]:
        """Send one batch and validate its response, preserving transport errors."""
        body: dict[str, object] = {
            "query": query,
            "candidates": [
                {_SCORE_RANK_KEY: rank, "text": text} for rank, text in batch
            ],
        }
        try:
            response = await self._client.post(self._endpoint, json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError:
            log_safely(logger, logging.WARNING, "HTTP rerank request failed")
            raise
        except json.JSONDecodeError as exc:
            log_safely(logger, logging.WARNING, "HTTP rerank returned invalid JSON", exc_info=exc)
            raise ValueError("HTTP rerank response is not valid JSON") from exc
        return _validate_batch_response(payload, expected_ranks)


__all__ = ["HTTPMemoryReranker"]

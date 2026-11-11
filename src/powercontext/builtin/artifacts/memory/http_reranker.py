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

"""HTTP cross-encoder reranking for coarse Memory search candidates."""

from __future__ import annotations

import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

DEFAULT_HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Score coarse candidates with an HTTP service, one bounded batch at a time.

    Global candidate ranks are preserved across batches: the service always sees the
    one-based rank from the full candidate pool, and the final selection is computed
    once over the whole pool rather than per batch.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = DEFAULT_HTTP_RERANK_POLICY_ID,
    ) -> None:
        if not endpoint:
            raise ValueError("rerank endpoint must not be empty")  # noqa: TRY003
        if batch_size < 1:
            raise ValueError("rerank batch_size must be positive")  # noqa: TRY003
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
        """Return the highest scoring original ranks in global score order."""

        candidate_count = len(candidates)
        if candidate_count < 1:
            raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
        if limit < 1 or limit > candidate_count:
            raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003

        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, candidate_count, self._batch_size):
            batch = candidates[start : start + self._batch_size]
            ranks = tuple(range(start + 1, start + 1 + len(batch)))
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": rank, "text": candidate.text}
                        for rank, candidate in zip(ranks, batch, strict=True)
                    ],
                },
            )
            requests += 1
            response.raise_for_status()
            scores.update(_parse_scores(response, expected_ranks=ranks))

        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return MemoryRerankDecision(
            selected_ranks=tuple(rank for rank, _ in ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _parse_scores(response: httpx.Response, *, expected_ranks: tuple[int, ...]) -> dict[int, float]:
    try:
        payload: Any = response.json()
    except ValueError:
        raise ValueError("rerank response is not valid JSON") from None

    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("rerank response must contain exactly the scores field")  # noqa: TRY003
    items = payload["scores"]
    if not isinstance(items, list):
        raise ValueError("rerank scores must be a list")  # noqa: TRY003

    allowed = set(expected_ranks)
    parsed: dict[int, float] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"rank", "score"}:
            raise ValueError("each rerank score must contain exactly rank and score")  # noqa: TRY003
        rank = item["rank"]
        score = item["score"]
        if isinstance(rank, bool) or not isinstance(rank, int):
            raise ValueError("rerank rank must be an integer")  # noqa: TRY003
        if rank not in allowed:
            raise ValueError("rerank score refers to a rank outside this batch")  # noqa: TRY003
        if rank in parsed:
            raise ValueError("rerank scores must refer to each batch rank exactly once")  # noqa: TRY003
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise ValueError("rerank score must be a number")  # noqa: TRY003
        value = float(score)
        if not math.isfinite(value):
            raise ValueError("rerank score must be finite")  # noqa: TRY003
        parsed[rank] = value

    if set(parsed) != allowed:
        raise ValueError("rerank scores must cover every batch rank exactly once")  # noqa: TRY003
    return parsed


__all__ = ["DEFAULT_HTTP_RERANK_POLICY_ID", "HTTPMemoryReranker"]

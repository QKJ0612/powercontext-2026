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

"""Cross-call Embedding request coalescing with a bounded result cache.

``CoalescingEmbeddingModel`` sits between an :class:`EmbeddingModel` and its
callers. Concurrent callers that need the same text share one underlying
provider request, successful vectors are kept in a bounded LRU cache, and each
caller still receives vectors in its own input order, including duplicates.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING

from powercontext.builtin.inference.models import (
    EmbeddingResult,
    EmbeddingVector,
    InferenceUsage,
)

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
    from powercontext.builtin.inference.protocols import EmbeddingModel

__all__ = ["CoalescingEmbeddingModel"]


def _drain_future_exception(future: asyncio.Future[EmbeddingVector]) -> None:
    """Mark a future exception as observed so abandoned waiters do not leak warnings."""

    if not future.cancelled():
        future.exception()


def _try_set_result(future: asyncio.Future[EmbeddingVector], vector: EmbeddingVector) -> None:
    """Resolve a waiter unless it or its event loop has already gone away."""

    if future.done() or future.get_loop().is_closed():
        return
    try:
        future.set_result(vector)
    except RuntimeError:  # the loop can close while a cancelled build is unwinding
        return


def _try_set_exception(future: asyncio.Future[EmbeddingVector], error: BaseException) -> None:
    """Fail a waiter unless it or its event loop has already gone away."""

    if future.done() or future.get_loop().is_closed():
        return
    try:
        future.set_exception(error)
    except RuntimeError:  # the loop can close while a cancelled build is unwinding
        return


class _Slot:
    """One distinct text waiting for (or joined to) one underlying batch."""

    __slots__ = ("batch", "future", "text", "waiters")

    def __init__(self, text: str) -> None:
        self.text = text
        self.batch: _Batch | None = None
        self.waiters = 0
        self.future: asyncio.Future[EmbeddingVector] = asyncio.get_running_loop().create_future()
        self.future.add_done_callback(_drain_future_exception)


class _Batch:
    """One provider request whose result resolves every slot it owns."""

    __slots__ = ("slots", "texts", "usage")

    def __init__(self, slots: tuple[_Slot, ...]) -> None:
        self.slots = slots
        self.texts = tuple(slot.text for slot in slots)
        self.usage = InferenceUsage(requests=0)


class CoalescingEmbeddingModel:
    """Deduplicate concurrent embedding work and cache successful vectors.

    Args:
        model: The underlying embedding model to wrap.
        max_batch_size: Maximum number of distinct texts sent in one provider request.
        coalesce_ms: Milliseconds to gather concurrent callers before dispatching.
        cache_size: Maximum number of cached vectors; zero disables cross-call caching.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be at least 1")
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must not be negative")
        if cache_size < 0:
            raise ValueError("cache_size must not be negative")

        self._model = model
        self.profile: EmbeddingProfile = model.profile
        self._max_batch_size = int(max_batch_size)
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = int(cache_size)
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._queued: dict[str, _Slot] = {}
        self._inflight: dict[str, _Slot] = {}
        self._flush_handle: asyncio.Handle | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one ordered vector per input text, coalescing shared work."""

        self._bind_loop()
        requested = tuple(texts)
        if not requested:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        unique = tuple(dict.fromkeys(requested))
        resolved: dict[str, EmbeddingVector] = {}
        slots: list[_Slot] = []
        for text in unique:
            cached = self._cache.get(text)
            if cached is not None:
                self._cache.move_to_end(text)
                resolved[text] = cached
                continue
            slot = self._inflight.get(text)
            if slot is None:
                slot = self._queued.get(text)
                if slot is None:
                    slot = _Slot(text)
                    self._queued[text] = slot
                    self._schedule_flush()
            slot.waiters += 1
            slots.append(slot)

        if slots:
            try:
                outcomes = await asyncio.gather(
                    *(asyncio.shield(slot.future) for slot in slots),
                    return_exceptions=True,
                )
            finally:
                # A cancelled call must not tear down a batch other callers still
                # wait on; it only stops waiting itself.
                self._release(slots)
            for slot, outcome in zip(slots, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    raise outcome
                resolved[slot.text] = outcome

        usage = _sum_usage(slots)
        return EmbeddingResult(
            vectors=tuple(resolved[text] for text in requested),
            usage=usage,
        )

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            # Pending slots and timers are bound to the previous event loop and
            # cannot be awaited from a new one; the cache itself is portable.
            self._loop = loop
            self._queued.clear()
            self._inflight.clear()
            self._flush_handle = None
            self._tasks.clear()
        return loop

    def _schedule_flush(self) -> None:
        if self._flush_handle is not None:
            return
        loop = asyncio.get_running_loop()
        if self._coalesce_seconds <= 0:
            # Defer by one loop iteration so callers started in the same turn
            # join the same wave even when the coalescing window is zero.
            self._flush_handle = loop.call_soon(self._flush)
        else:
            self._flush_handle = loop.call_later(self._coalesce_seconds, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        if not self._queued:
            return
        queued = self._queued
        self._queued = {}
        slots = [slot for slot in queued.values() if slot.waiters > 0]
        loop = asyncio.get_running_loop()
        for start in range(0, len(slots), self._max_batch_size):
            batch = _Batch(tuple(slots[start : start + self._max_batch_size]))
            for slot in batch.slots:
                slot.batch = batch
                self._inflight[slot.text] = slot
            task = loop.create_task(self._run_batch(batch))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_batch(self, batch: _Batch) -> None:
        try:
            result = await self._model.embed(batch.texts)
            vectors = tuple(result.vectors)
            if len(vectors) != len(batch.texts):
                raise ValueError(
                    f"embedding provider returned {len(vectors)} vectors for {len(batch.texts)} texts"
                )
        except asyncio.CancelledError as error:
            self._retire(batch)
            self._fail(batch, error)
            raise
        except BaseException as error:
            self._retire(batch)
            self._fail(batch, error)
            return

        self._retire(batch)
        if self._cache_size > 0:
            for text, vector in zip(batch.texts, vectors, strict=True):
                self._cache[text] = vector
                self._cache.move_to_end(text)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        batch.usage = result.usage
        for slot, vector in zip(batch.slots, vectors, strict=True):
            _try_set_result(slot.future, vector)

    def _retire(self, batch: _Batch) -> None:
        for slot in batch.slots:
            if self._inflight.get(slot.text) is slot:
                del self._inflight[slot.text]

    @staticmethod
    def _fail(batch: _Batch, error: BaseException) -> None:
        for slot in batch.slots:
            _try_set_exception(slot.future, error)

    def _release(self, slots: list[_Slot]) -> None:
        for slot in slots:
            if slot.waiters > 0:
                slot.waiters -= 1
            if slot.waiters == 0 and slot.batch is None and self._queued.get(slot.text) is slot:
                # Nobody is waiting for this text in the current coalescing
                # window, so there is no reason to spend a provider request.
                del self._queued[slot.text]


def _sum_usage(slots: list[_Slot]) -> InferenceUsage:
    requests = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    seen: set[int] = set()
    for slot in slots:
        batch = slot.batch
        if batch is None or id(batch) in seen:
            continue
        seen.add(id(batch))
        usage = batch.usage
        requests += usage.requests
        if usage.input_tokens is not None:
            input_tokens = usage.input_tokens if input_tokens is None else input_tokens + usage.input_tokens
        if usage.output_tokens is not None:
            output_tokens = usage.output_tokens if output_tokens is None else output_tokens + usage.output_tokens
    return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)

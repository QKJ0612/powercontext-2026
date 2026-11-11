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

"""Propagate Source lifecycle availability through a Scope's evidence DAG."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.errors import RepositoryNotFoundError
from powercontext.builtin.persistence.tables import (
    SOURCE_LIFECYCLE_EVENTS_TABLE,
    SOURCE_LIFECYCLE_NODES_TABLE,
)
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import ArtifactNotFoundError
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.persistence.database import AsyncDatabase
    from powercontext.builtin.runtime.application import BuiltinRuntime

_NodeKind = Literal["source", "memory", "topic"]
_Policy = Literal["all", "any"]
_PURPOSES = frozenset({"generation", "audit"})
_MAX_NODE_ID_LENGTH = 256


class SourceLifecycleError(ValueError):
    """Report a lifecycle request that violates the registered DAG or event contract."""

    _MESSAGES = {
        "conflict": "the node identity is already bound to a different registration",
        "invalid-node": "the node identity must be non-blank text",
        "invalid-parents": "derived nodes require at least one distinct registered parent",
        "invalid-policy": "the dependency policy must be either 'all' or 'any'",
        "invalid-reference": "the registration reference has the wrong type",
        "invalid-revision": "the lifecycle revision must be a positive integer",
        "lifecycle-database": "the runtime was not assembled with lifecycle persistence",
        "missing-memory-entry": "the registered Memory citation is not readable",
        "missing-parent": "a declared parent node has not been registered",
        "missing-reference": "the registered reference does not exist in this scope",
        "missing-source": "the event does not name a registered Source",
        "revision-mismatch": "the event revision does not match the Source lifecycle version",
        "sequence-conflict": "the event sequence was already used for different content",
        "stale-sequence": "the event sequence is behind the last applied event",
        "unavailable": "the node is not available for generation",
        "unknown-node": "the node has not been registered in this scope",
        "unknown-purpose": "the requested resolution purpose is not supported",
    }

    def __init__(self, code: str, detail: object | None = None) -> None:
        message = self._MESSAGES.get(code, code)
        super().__init__(message if detail is None else f"{message}: {detail}")
        self.code = code


class SourceLifecycleEvent(BaseModel):
    """One strictly validated Source lifecycle event."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str
    revision: StrictInt = Field(gt=0)
    sequence: StrictInt = Field(gt=0)
    available: StrictBool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")  # noqa: TRY003
        return value


@dataclass(frozen=True, slots=True)
class _LifecycleNode:
    node_id: str
    ordinal: int
    kind: _NodeKind
    available: bool
    revision: int | None
    reference: object
    parents: tuple[str, ...]
    policy: _Policy
    entry_id: str | None
    entry_version_id: str | None
    artifact_id: str | None
    artifact_revision: int | None
    last_sequence: int


def _event_digest(event: SourceLifecycleEvent) -> str:
    payload = json.dumps(
        [event.node_id, event.revision, event.sequence, event.available, event.reason],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _decode_reference(kind: str, value: str) -> object:
    if kind == "source":
        return SourceRef.model_validate_json(value)
    if kind == "memory":
        return MemoryCitation.model_validate_json(value)
    return ArtifactRef.model_validate_json(value)


def _encode_reference(reference: object) -> str:
    encode = getattr(reference, "model_dump_json", None)
    if encode is None:
        raise SourceLifecycleError("invalid-reference", type(reference).__name__)
    return str(encode())


def _node_from_row(row: Any) -> _LifecycleNode:
    kind = str(row["kind"])
    return _LifecycleNode(
        node_id=str(row["node_id"]),
        ordinal=int(row["ordinal"]),
        kind=kind,  # type: ignore[arg-type]
        available=bool(row["available"]),
        revision=None if row["revision"] is None else int(row["revision"]),
        reference=_decode_reference(kind, str(row["reference_json"])),
        parents=tuple(json.loads(str(row["parents_json"]))),
        policy=str(row["policy"]),  # type: ignore[arg-type]
        entry_id=None if row["entry_id"] is None else str(row["entry_id"]),
        entry_version_id=(
            None if row["entry_version_id"] is None else str(row["entry_version_id"])
        ),
        artifact_id=None if row["reference_artifact_id"] is None else str(row["reference_artifact_id"]),
        artifact_revision=(
            None if row["reference_artifact_revision"] is None else int(row["reference_artifact_revision"])
        ),
        last_sequence=int(row["last_sequence"]),
    )


def _normalize_parents(parents: Iterable[str]) -> tuple[str, ...]:
    if isinstance(parents, str):
        raise SourceLifecycleError("invalid-parents", "parents must be a sequence of node ids")
    values = tuple(parents)
    if not values:
        raise SourceLifecycleError("invalid-parents")
    seen: set[str] = set()
    for parent in values:
        if not isinstance(parent, str) or not parent.strip():
            raise SourceLifecycleError("invalid-parents", parent)
        if parent in seen:
            raise SourceLifecycleError("invalid-parents", f"duplicate parent {parent!r}")
        seen.add(parent)
    return values


def _normalize_policy(policy: str) -> _Policy:
    if policy not in {"all", "any"}:
        raise SourceLifecycleError("invalid-policy", policy)
    return policy  # type: ignore[return-value]


def _normalize_node_id(node_id: str) -> str:
    if not isinstance(node_id, str) or not node_id.strip() or len(node_id) > _MAX_NODE_ID_LENGTH:
        raise SourceLifecycleError("invalid-node", node_id)
    return node_id


def _normalize_revision(revision: int) -> int:
    if type(revision) is not int or revision < 1:
        raise SourceLifecycleError("invalid-revision", revision)
    return revision


class SourceLifecycleApplication:
    """Select the lifecycle service for one Scope."""

    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleApplication:
        return ScopedSourceLifecycleApplication(self._runtime, scope_id)


class ScopedSourceLifecycleApplication:
    """Register evidence nodes and propagate Source events in one Scope."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)

    # -- registration -------------------------------------------------

    async def register_source(self, node_id: str, reference: SourceRef, *, revision: int) -> None:
        node = _normalize_node_id(node_id)
        version = _normalize_revision(revision)
        if not isinstance(reference, SourceRef):
            raise SourceLifecycleError("invalid-reference", type(reference).__name__)
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._read_nodes()
            existing = self._find(nodes, node)
            if existing is not None:
                if (
                    existing.kind == "source"
                    and existing.revision == version
                    and existing.reference == reference
                ):
                    return
                raise SourceLifecycleError("conflict", node)
            await self._require_source(reference)
            await self._insert_node(
                node_id=node,
                ordinal=self._next_ordinal(nodes),
                kind="source",
                available=True,
                revision=version,
                reference=reference,
                parents=(),
                policy="all",
                entry_id=None,
                entry_version_id=None,
                artifact_id=None,
                artifact_revision=None,
            )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: Sequence[str],
        policy: _Policy,
    ) -> None:
        node = _normalize_node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("invalid-reference", type(citation).__name__)
        parent_ids = _normalize_parents(parents)
        selected_policy = _normalize_policy(policy)
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._read_nodes()
            existing = self._find(nodes, node)
            if existing is not None:
                if (
                    existing.kind == "memory"
                    and existing.reference == citation
                    and existing.parents == parent_ids
                    and existing.policy == selected_policy
                ):
                    await self._apply_memory_states(
                        ((citation.entry_id, citation.entry_version_id, existing.available),)
                    )
                    return
                raise SourceLifecycleError("conflict", node)
            await self._require_parents(nodes, parent_ids)
            await self._require_memory_citation(citation)
            available = self._derive(nodes, parent_ids, selected_policy)
            await self._apply_memory_states(
                ((citation.entry_id, citation.entry_version_id, available),)
            )
            await self._insert_node(
                node_id=node,
                ordinal=self._next_ordinal(nodes),
                kind="memory",
                available=available,
                revision=None,
                reference=citation,
                parents=parent_ids,
                policy=selected_policy,
                entry_id=citation.entry_id,
                entry_version_id=citation.entry_version_id,
                artifact_id=citation.memory_ref.artifact_id,
                artifact_revision=citation.memory_ref.revision,
            )

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        *,
        parents: Sequence[str],
        policy: _Policy,
    ) -> None:
        node = _normalize_node_id(node_id)
        if not isinstance(reference, ArtifactRef) or reference.family != "topic-memory":
            raise SourceLifecycleError("invalid-reference", type(reference).__name__)
        parent_ids = _normalize_parents(parents)
        selected_policy = _normalize_policy(policy)
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._read_nodes()
            existing = self._find(nodes, node)
            if existing is not None:
                if (
                    existing.kind == "topic"
                    and existing.reference == reference
                    and existing.parents == parent_ids
                    and existing.policy == selected_policy
                ):
                    return
                raise SourceLifecycleError("conflict", node)
            await self._require_parents(nodes, parent_ids)
            await self._require_topic(reference)
            await self._insert_node(
                node_id=node,
                ordinal=self._next_ordinal(nodes),
                kind="topic",
                available=self._derive(nodes, parent_ids, selected_policy),
                revision=None,
                reference=reference,
                parents=parent_ids,
                policy=selected_policy,
                entry_id=None,
                entry_version_id=None,
                artifact_id=reference.artifact_id,
                artifact_revision=reference.revision,
            )

    # -- events and resolution ---------------------------------------

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            event = SourceLifecycleEvent.model_validate(event)
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._read_nodes()
            source = self._find(nodes, event.node_id)
            if source is None or source.kind != "source":
                raise SourceLifecycleError("missing-source", event.node_id)
            if source.revision != event.revision:
                raise SourceLifecycleError(
                    "revision-mismatch",
                    f"{event.revision} != {source.revision}",
                )
            if event.sequence < source.last_sequence:
                raise SourceLifecycleError("stale-sequence", event.sequence)
            if event.sequence == source.last_sequence:
                stored = await self._read_event(event.node_id, event.sequence)
                if stored is not None and self._same_event(stored, event):
                    return sorted(node.node_id for node in nodes if node.available)
                raise SourceLifecycleError("sequence-conflict", event.sequence)

            available = {node.node_id: node.available for node in nodes}
            available[event.node_id] = event.available
            for node in sorted(nodes, key=lambda item: item.ordinal):
                if node.kind == "source":
                    continue
                available[node.node_id] = self._derive_from_map(available, node.parents, node.policy)

            await self._sync_memory(nodes, available)
            await self._persist_event(nodes, available, event)
            return sorted(node_id for node_id, state in available.items() if state)

    async def resolve(self, node_id: str, purpose: str = "generation") -> object:
        if purpose not in _PURPOSES:
            raise SourceLifecycleError("unknown-purpose", purpose)
        nodes = await self._read_nodes()
        node = self._find(nodes, node_id)
        if node is None:
            raise SourceLifecycleError("unknown-node", node_id)
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError("unavailable", node_id)
        return node.reference

    async def unavailable_topic_refs(self) -> frozenset[tuple[str, int]]:
        """Return exact Topic revisions whose registered node is currently not supported."""

        async with self._database.transaction() as connection:
            rows = await connection.execute(
                select(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.reference_artifact_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.reference_artifact_revision,
                ).where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.kind == "topic",
                    SOURCE_LIFECYCLE_NODES_TABLE.c.available.is_(False),
                )
            )
            return frozenset(
                (str(artifact_id), int(revision))
                for artifact_id, revision in rows
                if artifact_id is not None and revision is not None
            )

    # -- persistence --------------------------------------------------

    @property
    def _database(self) -> AsyncDatabase:
        contexts = getattr(self._runtime, "_lifecycle_contexts", None)
        if contexts is None:
            raise SourceLifecycleError("lifecycle-database")
        return contexts.database

    @property
    def _repositories(self) -> Any:
        contexts = getattr(self._runtime, "_lifecycle_contexts", None)
        if contexts is None:
            raise SourceLifecycleError("lifecycle-database")
        return contexts.repositories

    @staticmethod
    def _find(nodes: Sequence[_LifecycleNode], node_id: str) -> _LifecycleNode | None:
        return next((node for node in nodes if node.node_id == node_id), None)

    @staticmethod
    def _next_ordinal(nodes: Sequence[_LifecycleNode]) -> int:
        return max((node.ordinal for node in nodes), default=0) + 1

    async def _read_nodes(self) -> tuple[_LifecycleNode, ...]:
        async with self._database.transaction() as connection:
            return await self._select_nodes(connection)

    async def _select_nodes(self, connection: AsyncConnection) -> tuple[_LifecycleNode, ...]:
        result = await connection.execute(
            select(SOURCE_LIFECYCLE_NODES_TABLE)
            .where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id)
            .order_by(SOURCE_LIFECYCLE_NODES_TABLE.c.ordinal)
        )
        return tuple(_node_from_row(row) for row in result.mappings())

    async def _read_event(self, node_id: str, sequence: int) -> Any:
        async with self._database.transaction() as connection:
            result = await connection.execute(
                select(SOURCE_LIFECYCLE_EVENTS_TABLE).where(
                    SOURCE_LIFECYCLE_EVENTS_TABLE.c.scope_id == self.scope_id,
                    SOURCE_LIFECYCLE_EVENTS_TABLE.c.node_id == node_id,
                    SOURCE_LIFECYCLE_EVENTS_TABLE.c.sequence == sequence,
                )
            )
            return result.mappings().one_or_none()

    @staticmethod
    def _same_event(stored: Any, event: SourceLifecycleEvent) -> bool:
        return (
            int(stored["revision"]) == event.revision
            and bool(stored["available"]) == event.available
            and str(stored["reason"]) == event.reason
        )

    async def _insert_node(self, **values: object) -> None:
        parents = values.pop("parents")
        reference = values.pop("reference")
        values["reference_artifact_id"] = values.pop("artifact_id")
        values["reference_artifact_revision"] = values.pop("artifact_revision")
        reference_json = _encode_reference(reference)
        parent_values = list(parents) if isinstance(parents, tuple) else []
        async with self._database.transaction() as connection:
            await connection.execute(
                insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                    scope_id=self.scope_id,
                    reference_json=reference_json,
                    parents_json=json.dumps(parent_values, ensure_ascii=False, separators=(",", ":")),
                    last_sequence=0,
                    last_event_digest=None,
                    **values,
                )
            )

    async def _persist_event(
        self,
        nodes: Sequence[_LifecycleNode],
        available: dict[str, bool],
        event: SourceLifecycleEvent,
    ) -> None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._database.transaction() as connection:
            for node in nodes:
                state = available[node.node_id]
                if state != node.available:
                    await connection.execute(
                        update(table)
                        .where(table.c.scope_id == self.scope_id, table.c.node_id == node.node_id)
                        .values(available=state)
                    )
            await connection.execute(
                update(table)
                .where(table.c.scope_id == self.scope_id, table.c.node_id == event.node_id)
                .values(last_sequence=event.sequence, last_event_digest=_event_digest(event))
            )
            await connection.execute(
                insert(SOURCE_LIFECYCLE_EVENTS_TABLE).values(
                    scope_id=self.scope_id,
                    node_id=event.node_id,
                    sequence=event.sequence,
                    revision=event.revision,
                    available=event.available,
                    reason=event.reason,
                    digest=_event_digest(event),
                )
            )

    # -- validation ---------------------------------------------------

    async def _require_source(self, reference: SourceRef) -> None:
        try:
            async with self._database.transaction() as connection:
                await self._repositories.sources.get(connection, self.scope_id, reference)
        except RepositoryNotFoundError as error:
            raise SourceLifecycleError("missing-reference", reference) from error

    async def _require_topic(self, reference: ArtifactRef) -> None:
        try:
            async with self._database.transaction() as connection:
                artifact = await self._repositories.artifacts.get(connection, self.scope_id, reference)
        except RepositoryNotFoundError as error:
            raise SourceLifecycleError("missing-reference", reference) from error
        if artifact.family != "topic-memory" or artifact.as_ref() != reference:
            raise SourceLifecycleError("invalid-reference", reference)

    async def _require_memory_citation(self, citation: MemoryCitation) -> None:
        if citation.memory_ref.family != "memory":
            raise SourceLifecycleError("invalid-reference", citation.memory_ref)
        try:
            async with self._database.transaction() as connection:
                artifact = await self._repositories.artifacts.get(
                    connection, self.scope_id, citation.memory_ref
                )
        except RepositoryNotFoundError as error:
            raise SourceLifecycleError("missing-memory-entry", citation) from error
        if artifact.family != "memory":
            raise SourceLifecycleError("missing-memory-entry", citation)
        item = next(
            (
                entry
                for entry in artifact.content.manifest.entries
                if entry.entry_id == citation.entry_id
            ),
            None,
        )
        if item is None or item.entry_version_id != citation.entry_version_id:
            raise SourceLifecycleError("missing-memory-entry", citation.entry_id)

    @staticmethod
    async def _require_parents(nodes: Sequence[_LifecycleNode], parents: Sequence[str]) -> None:
        registered = {node.node_id for node in nodes}
        for parent in parents:
            if parent not in registered:
                raise SourceLifecycleError("missing-parent", parent)

    @staticmethod
    def _derive(
        nodes: Sequence[_LifecycleNode],
        parents: Sequence[str],
        policy: _Policy,
    ) -> bool:
        states = {node.node_id: node.available for node in nodes}
        return ScopedSourceLifecycleApplication._derive_from_map(states, tuple(parents), policy)

    @staticmethod
    def _derive_from_map(
        available: dict[str, bool],
        parents: Sequence[str],
        policy: _Policy,
    ) -> bool:
        support = [available[parent] for parent in parents]
        return all(support) if policy == "all" else any(support)

    # -- native Memory state ------------------------------------------

    async def _sync_memory(
        self,
        nodes: Sequence[_LifecycleNode],
        available: dict[str, bool],
    ) -> None:
        states = tuple(
            (
                node.entry_id,
                node.entry_version_id,
                available[node.node_id],
            )
            for node in nodes
            if node.kind == "memory"
        )
        await self._apply_memory_states(states)

    async def _apply_memory_states(
        self,
        states: Sequence[tuple[str | None, str | None, bool]],
    ) -> None:
        if not states:
            return
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            artifact_id = context.artifacts.memory_artifact_id
            head = await self._memory_head(service, artifact_id)
            if head is None:
                raise SourceLifecycleError("missing-memory-entry", artifact_id)
            versions = {entry.entry_id: entry for entry in await service.entries(head)}
            current = {item.entry_id: item.state for item in head.content.manifest.entries}
            deactivate = []
            activate = []
            for entry_id, entry_version_id, desired in states:
                if entry_id is None or entry_version_id is None:
                    raise SourceLifecycleError("missing-memory-entry", entry_id)
                version = versions.get(entry_id)
                if version is None or version.entry_version_id != entry_version_id:
                    raise SourceLifecycleError("missing-memory-entry", entry_id)
                state = current.get(entry_id)
                if desired and state != "active":
                    activate.append(version)
                elif not desired and state != "inactive":
                    deactivate.append(version)
            if deactivate:
                head = await self._memory_head(service, artifact_id)
                if head is None:
                    raise SourceLifecycleError("missing-memory-entry", artifact_id)
                await service.forget(head, entries=tuple(deactivate), reason="source-lifecycle")
            if activate:
                head = await self._memory_head(service, artifact_id)
                if head is None:
                    raise SourceLifecycleError("missing-memory-entry", artifact_id)
                await service.reactivate(head, entries=tuple(activate), reason="source-lifecycle")

    @staticmethod
    async def _memory_head(service: Any, artifact_id: str) -> Any:
        try:
            return await service.head(artifact_id)
        except ArtifactNotFoundError:
            return None


__all__ = [
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "ScopedSourceLifecycleApplication",
]

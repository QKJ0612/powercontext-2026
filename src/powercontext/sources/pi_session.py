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

"""Pi session branch capture: one immutable Source per visible event."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    AdapterSourceDefinition,
    ConnectorRunCompletion,
    ConnectorRunStatus,
    ConnectorRunSession,
    ConnectorSubmissionStatus,
    Source,
    SourceMaterialization,
    TEXT_EVIDENCE_PROJECTION_KEY,
    TextEvidence,
)

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"
_CHECKPOINT_VERSION = 1


class PiSessionInput(BaseModel):
    """One complete Pi v3 JSONL session and the selected branch leaf."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, with its full original record."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


class PiEventSource(Source):
    """A captured Pi event whose identity is the session id plus the event id."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build the stable, delimiter-safe identity of one Pi event Source."""

    identity = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"pi event source id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _block_text(blocks: object) -> str:
    """Join visible text blocks in order, rendering tool calls compactly."""

    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind == "toolCall":
            name = block.get("name")
            call_id = block.get("id")
            parts.append(f"[tool_call {name} {call_id}] {_compact_json(block.get('arguments'))}")
    return "\n".join(parts)


def _visible_body(record: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(role, body)`` for an event that carries visible text, else ``None``."""

    event_type = record.get("type")
    if not isinstance(event_type, str):
        return None

    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        if role == "toolResult":
            name = message.get("toolName")
            call_id = message.get("toolCallId")
            text = _block_text(message.get("content"))
            detail = f"[tool_result {name} {call_id}]"
            return role, f"{detail} {text}" if text else detail
        if role == "bashExecution":
            command = message.get("command")
            output = message.get("output")
            return role, f"$ {command}\n{output}"
        return role, _block_text(message.get("content"))

    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str):
            return None
        return event_type, summary

    if event_type == "custom_message":
        return "custom", _block_text(record.get("content"))

    return None


def _read_records(content: bytes) -> tuple[str, dict[str, dict[str, Any]], list[str]]:
    """Decode the archive and validate the parent graph atomically."""

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("pi session must be valid UTF-8") from error

    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise ValueError("pi session lines must be valid JSON") from error
        if not isinstance(record, dict):
            raise ValueError("pi session records must be JSON objects")  # noqa: TRY003
        records.append(record)

    if not records:
        raise ValueError("pi session must start with a session header")  # noqa: TRY003
    header = records[0]
    if header.get("type") != "session" or header.get("version") != 3:
        raise ValueError("pi session header must declare type=session and version=3")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("pi session header must declare a non-empty id")  # noqa: TRY003

    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records[1:]:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("pi session records must declare a non-empty id")  # noqa: TRY003
        if event_id in by_id:
            raise ValueError(f"duplicate pi session record id {event_id!r}")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id is not None and parent_id not in by_id:
            raise ValueError(f"pi session record {event_id!r} references an unknown parent")  # noqa: TRY003
        by_id[event_id] = record
        order.append(event_id)

    return session_id, by_id, order


def _branch(session_id: str, by_id: dict[str, dict[str, Any]], order: list[str], leaf_id: str | None) -> list[dict[str, Any]]:
    if not by_id:
        if leaf_id is not None:
            raise ValueError("pi session does not contain the selected leaf")  # noqa: TRY003
        return []
    leaf = leaf_id if leaf_id is not None else order[-1]
    if leaf not in by_id:
        raise ValueError(f"pi session does not contain leaf {leaf!r}")  # noqa: TRY003

    chain: list[dict[str, Any]] = []
    visited: set[str] = set()
    current: str | None = leaf
    while current is not None:
        if current in visited:
            raise ValueError("pi session parent graph contains a cycle")  # noqa: TRY003
        visited.add(current)
        record = by_id[current]
        chain.append(record)
        current = record.get("parentId")
    chain.reverse()
    return chain


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Return the visible events on the selected branch, ordered root to leaf."""

    session_id, by_id, order = _read_records(value.content)
    chain = _branch(session_id, by_id, order, value.leaf_id)

    events: list[PiEventInput] = []
    for record in chain:
        visible = _visible_body(record)
        if visible is None:
            continue
        role, body = visible
        if not body:
            continue
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=record["id"],
                parent_id=record.get("parentId"),
                event_type=record["type"],
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        )
    return tuple(events)


class PiEventSourceAdapter:
    """Resolve and read Pi event Sources."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        identity = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
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


class PiEventTextEvidenceProjection:
    """Expose one Pi event as standard textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    projections=(PiEventTextEvidenceProjection(),),
)


def _event_digest(event: PiEventInput) -> str:
    payload = _compact_json({"record": event.record, "content": event.content})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _imported_events(checkpoint: object, session_id: str) -> dict[str, str]:
    if not isinstance(checkpoint, dict):
        return {}
    stored_session = checkpoint.get("session_id")
    if isinstance(stored_session, str) and stored_session != session_id:
        return {}
    entries = checkpoint.get("events")
    if not isinstance(entries, dict):
        return {}
    return {key: value for key, value in entries.items() if isinstance(key, str) and isinstance(value, str)}


class PiSessionConnector:
    """Import only the events this branch added since the committed progress."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = self._path.read_bytes()
        session_id, by_id, order = _read_records(content)
        events = parse_pi_session(PiSessionInput(content=content, leaf_id=self._leaf_id))
        del by_id, order

        imported = _imported_events(session.checkpoint, session_id)
        pending = dict(imported)
        for event in events:
            digest = _event_digest(event)
            known = imported.get(event.event_id)
            if known is not None and known != digest:
                raise ValueError(f"pi session event {event.event_id!r} changed after import")  # noqa: TRY003
            if known is not None:
                continue
            outcome = await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            if outcome.status is not ConnectorSubmissionStatus.ACCEPTED:
                return ConnectorRunCompletion(status=ConnectorRunStatus.INCOMPLETE, checkpoint=None)
            pending[event.event_id] = digest

        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={
                "version": _CHECKPOINT_VERSION,
                "session_id": session_id,
                "events": pending,
            },
        )


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]

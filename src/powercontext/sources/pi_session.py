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

"""Capture the visible events of one selected Pi session branch as raw evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_VERSION = 3
_CHECKPOINT_KEY = "events"


class PiSessionInput(BaseModel):
    """One complete Pi session JSONL archive and an optional selected leaf."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event from the selected branch, with its complete source record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """A captured Pi session event addressed by session id and event id."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    identity = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"pi event source id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def _text_blocks(value: object) -> str:
    """Join every visible text block, ignoring thinking and image blocks."""

    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for block in value:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _tool_call(block: dict[str, Any]) -> str:
    name = block.get("name", "")
    call_id = block.get("id", "")
    arguments = block.get("arguments", {})
    compact = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"[tool_call {name} {call_id}] {compact}"


def _message_body(message: dict[str, Any]) -> tuple[str, str]:
    role = message.get("role")
    role_text = role if isinstance(role, str) else "unknown"

    if role_text == "toolResult":
        name = message.get("toolName", "")
        call_id = message.get("toolCallId", "")
        return role_text, f"[tool_result {name} {call_id}] {_text_blocks(message.get('content'))}"

    if role_text == "bashExecution":
        command = message.get("command", "")
        output = message.get("output", "")
        return role_text, f"$ {command}\n{output}"

    content = message.get("content")
    if isinstance(content, str):
        return role_text, content

    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif kind == "toolCall":
                parts.append(_tool_call(block))
    return role_text, "\n".join(parts)


def _event_body(record: dict[str, Any]) -> tuple[str, str] | None:
    """Return (role, body) for a visible event, or ``None`` for control records."""

    event_type = record.get("type")
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role, body = _message_body(message)
        return (role, body) if body else None
    if event_type in ("compaction", "branch_summary"):
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary:
            return None
        return str(event_type), summary
    if event_type == "custom_message":
        body = _text_blocks(record.get("content"))
        return ("custom", body) if body else None
    return None


def _load_records(content: bytes) -> list[dict[str, Any]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("pi session archive must be valid UTF-8") from error

    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise ValueError("pi session archive must contain one JSON object per line") from error
        if not isinstance(record, dict):
            raise ValueError("each pi session record must be a JSON object")  # noqa: TRY003
        records.append(record)
    if not records:
        raise ValueError("pi session archive must not be empty")  # noqa: TRY003
    return records


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Parse the selected branch and return its visible events from root to leaf."""

    records = _load_records(value.content)

    header = records[0]
    if header.get("type") != "session":
        raise ValueError("pi session archive must start with a session header")  # noqa: TRY003
    if header.get("version") != PI_SESSION_VERSION:
        raise ValueError(f"pi session version must be {PI_SESSION_VERSION}")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("pi session header must declare a non-empty id")  # noqa: TRY003

    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records[1:]:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("every pi session record must declare a non-empty id")  # noqa: TRY003
        if event_id in nodes:
            raise ValueError("pi session event ids must be unique")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or parent_id not in nodes:
                raise ValueError("every parentId must reference an already declared event")  # noqa: TRY003
        nodes[event_id] = record
        order.append(event_id)

    if not order:
        raise ValueError("pi session archive must contain at least one event")  # noqa: TRY003

    leaf = value.leaf_id if value.leaf_id is not None else order[-1]
    if leaf not in nodes:
        raise ValueError("selected leaf event does not exist")  # noqa: TRY003

    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = leaf
    while current is not None:
        if current in seen:
            raise ValueError("pi session branch must not contain a cycle")  # noqa: TRY003
        seen.add(current)
        chain.append(current)
        current = nodes[current].get("parentId")
    chain.reverse()

    events: list[PiEventInput] = []
    for event_id in chain:
        record = nodes[event_id]
        visible = _event_body(record)
        if visible is None:
            continue
        role, body = visible
        parent_id = record.get("parentId")
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=event_id,
                parent_id=parent_id if isinstance(parent_id, str) else None,
                event_type=str(record.get("type")),
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
    """Expose the original event text through the standard evidence projection."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
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


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    projections=(PiEventTextEvidenceProjection(),),
)


def _event_digest(event: PiEventInput) -> str:
    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PiSessionConnector:
    """Import new visible events of one Pi session branch and persist progress."""

    name = PI_SESSION_CONNECTOR_NAME
    version = "1"
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        events = parse_pi_session(
            PiSessionInput(content=self._path.read_bytes(), leaf_id=self._leaf_id)
        )

        imported: dict[str, str] = {}
        if isinstance(session.checkpoint, dict):
            stored = session.checkpoint.get(_CHECKPOINT_KEY)
            if isinstance(stored, dict):
                imported = {str(key): str(value) for key, value in stored.items()}

        pending: list[PiEventInput] = []
        for event in events:
            digest = _event_digest(event)
            known = imported.get(event.event_id)
            if known is None:
                pending.append(event)
            elif known != digest:
                raise ValueError(f"already imported pi event {event.event_id} was rewritten")  # noqa: TRY003

        for event in pending:
            await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)

        updated = dict(imported)
        for event in pending:
            updated[event.event_id] = _event_digest(event)
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={_CHECKPOINT_KEY: updated},
        )


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]

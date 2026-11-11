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

"""Wire format for bounded context packs and their incremental deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512


class ContextPackError(ValueError):
    """Raised when a pack or delta violates the wire contract."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(values: object) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError("items must be a list")
    decoded: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError("items must be base64 strings")
        try:
            decoded.append(base64.b64decode(value.encode("ascii"), validate=True))
        except (binascii.Error, UnicodeEncodeError, ValueError) as error:
            raise ContextPackError("items must be valid base64") from error
    return tuple(decoded)


def _require_digest(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        raise ContextPackError(f"{name} must be a sha256 digest")
    return value


@dataclass(frozen=True)
class ContextPack:
    """One bounded set of UTF-8 chunks plus its signed wire envelope."""

    items: tuple[bytes, ...]

    @classmethod
    def build(cls, items: tuple[bytes, ...], /) -> ContextPack:
        return cls(tuple(bytes(item) for item in items))

    @classmethod
    def from_content(cls, text: str, /, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")
        chunks: list[bytes] = []
        current = b""
        for character in text:
            encoded = character.encode("utf-8")
            if len(encoded) > chunk_bytes:
                raise ContextPackError("chunk_bytes cannot hold one UTF-8 character")
            if len(current) + len(encoded) > chunk_bytes:
                chunks.append(current)
                current = b""
            current += encoded
        if current:
            chunks.append(current)
        return cls(tuple(chunks))

    @classmethod
    def from_bytes(cls, raw: bytes, /) -> ContextPack:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ContextPackError("pack must be valid JSON") from error
        if not isinstance(payload, dict) or set(payload) != {"schema", "items", "digest", "byte_count"}:
            raise ContextPackError("pack must contain exactly schema, items, digest and byte_count")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("pack schema mismatch")
        items = _decode_items(payload["items"])
        pack = cls(items)
        digest = _require_digest("digest", payload["digest"])
        if digest != pack.digest:
            raise ContextPackError("pack digest mismatch")
        byte_count = payload["byte_count"]
        if isinstance(byte_count, bool) or not isinstance(byte_count, int):
            raise ContextPackError("byte_count must be an integer")
        if byte_count != len(_canonical(payload)):
            raise ContextPackError("byte_count mismatch")
        return pack

    @property
    def digest(self) -> str:
        unsigned = {"schema": PACK_SCHEMA, "items": _encode_items(self.items)}
        return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        unsigned = {"schema": PACK_SCHEMA, "items": _encode_items(self.items), "digest": self.digest}
        count = 0
        while True:
            raw = _canonical({**unsigned, "byte_count": count})
            if len(raw) == count:
                return raw
            count = len(raw)


@dataclass(frozen=True)
class ContextPackDelta:
    """A prefix-preserving delta from one pack to another."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @classmethod
    def from_bytes(cls, raw: bytes, /) -> ContextPackDelta:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ContextPackError("delta must be valid JSON") from error
        if not isinstance(payload, dict) or set(payload) != {
            "schema",
            "base_digest",
            "target_digest",
            "prefix_items",
            "appended",
        }:
            raise ContextPackError("delta fields are incomplete or unexpected")
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("delta schema mismatch")
        prefix = payload["prefix_items"]
        if isinstance(prefix, bool) or not isinstance(prefix, int) or prefix < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        return cls(
            base_digest=_require_digest("base_digest", payload["base_digest"]),
            target_digest=_require_digest("target_digest", payload["target_digest"]),
            prefix_items=prefix,
            appended=_decode_items(payload["appended"]),
        )

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Validate a delta against its base and rebuild the exact target pack."""

    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("base and delta must be pack values")
    if delta.base_digest != base.digest:
        raise ContextPackError("delta does not apply to this base pack")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base item count")
    rebuilt = ContextPack.build((*base.items[: delta.prefix_items], *delta.appended))
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("delta target digest mismatch")
    return rebuilt


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "DELTA_SCHEMA",
    "PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]

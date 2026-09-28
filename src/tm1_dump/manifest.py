"""Manifest describing the contents of a tm1-dump zip.

``manifest.json`` sits at the zip root and indexes everything the dump
engine wrote. Schema:

    {
      "schema_version": 1,
      "tool_version": "0.1.0",
      "created_at": "2026-09-29T01:40:00+00:00",    # UTC ISO
      "source": {"server": ..., "address": ..., "port": ..., "tm1py_version": ...},
      "filters": {...},
      "counts": {"dimensions": 12, "cubes": 3, ...},
      "objects": [{"type": ..., "name": ..., "file": ..., "sha256": ...}]
    }
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SourceInfo:
    """TM1 server the dump was taken from."""

    server: str
    address: str
    port: int
    tm1py_version: str


@dataclass
class ManifestObject:
    """One object stored in the zip, with its file and content hash."""

    type: str
    name: str
    file: str
    sha256: str


@dataclass
class Manifest:
    """Contents index for one dump zip."""

    schema_version: int = SCHEMA_VERSION
    tool_version: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    source: SourceInfo = field(default_factory=lambda: SourceInfo(server="", address="", port=0, tm1py_version=""))
    filters: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    objects: list[ManifestObject] = field(default_factory=list)

    def to_json(self) -> str:
        """Serialize the manifest to an indented JSON string."""
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        """Parse a manifest from a JSON string.

        Raises ``ValueError`` when the schema version is unknown and
        ``KeyError``/``TypeError`` for a structurally corrupt manifest.
        """
        data = json.loads(text)
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema_version: {data.get('schema_version')!r}")
        return cls(
            schema_version=data["schema_version"],
            tool_version=data["tool_version"],
            created_at=data["created_at"],
            source=SourceInfo(**data["source"]),
            filters=data["filters"],
            counts=data["counts"],
            objects=[ManifestObject(**entry) for entry in data["objects"]],
        )

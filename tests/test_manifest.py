"""Tests for Manifest JSON serialization."""

import json
from datetime import datetime

import pytest

from tm1_dump.manifest import SCHEMA_VERSION, Manifest, ManifestObject, SourceInfo


def _sample_manifest() -> Manifest:
    """Build a manifest exercising every schema field."""
    return Manifest(
        tool_version="0.1.0",
        source=SourceInfo(server="tm1srv01", address="prod.tm1", port=12354, tm1py_version="2.0.2"),
        filters={"include": {"dimensions": "^A.*"}},
        counts={"dimensions": 1, "cubes": 1},
        objects=[
            ManifestObject(type="dimensions", name="Account", file="dimensions/Account.json", sha256="ab" * 32),
            ManifestObject(type="cubes", name="P&L", file="cubes/P%26L.json", sha256="cd" * 32),
        ],
    )


def test_to_json_from_json_roundtrip():
    manifest = _sample_manifest()
    assert Manifest.from_json(manifest.to_json()) == manifest


def test_to_json_matches_schema():
    """The serialized keys must match the manifest schema exactly."""
    data = json.loads(_sample_manifest().to_json())
    assert data["schema_version"] == SCHEMA_VERSION
    assert set(data) == {
        "schema_version",
        "tool_version",
        "created_at",
        "source",
        "filters",
        "counts",
        "objects",
        "errors",
    }
    assert set(data["source"]) == {"server", "address", "port", "tm1py_version"}
    assert set(data["objects"][0]) == {"type", "name", "file", "sha256"}


def test_errors_roundtrip_and_default_empty():
    """errors serialize with the manifest and default to an empty list."""
    manifest = _sample_manifest()
    assert manifest.errors == []
    manifest.errors.append({"type": "dimensions", "name": "Broken", "error": "boom"})
    parsed = Manifest.from_json(manifest.to_json())
    assert parsed.errors == [{"type": "dimensions", "name": "Broken", "error": "boom"}]


def test_from_json_tolerates_manifest_without_errors():
    """Older manifests (or hand-written ones) without an errors key still load."""
    data = json.loads(_sample_manifest().to_json())
    del data["errors"]
    assert Manifest.from_json(json.dumps(data)).errors == []


def test_created_at_defaults_to_utc_iso():
    manifest = Manifest(tool_version="0.1.0", source=SourceInfo(server="s", address="a", port=1, tm1py_version="2.0.2"))
    parsed = datetime.fromisoformat(manifest.created_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_from_json_rejects_unknown_schema_version():
    with pytest.raises(ValueError, match="schema_version"):
        Manifest.from_json(json.dumps({"schema_version": SCHEMA_VERSION + 1}))


def test_from_json_rejects_corrupt_manifest():
    with pytest.raises(KeyError):
        Manifest.from_json(json.dumps({"schema_version": SCHEMA_VERSION}))

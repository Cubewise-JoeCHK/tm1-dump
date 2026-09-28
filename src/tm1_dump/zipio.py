"""Zip layout contract for tm1-dump archives.

Single source of truth for where each object lives inside a dump zip:
build paths from (object type, name[, parent]) and resolve paths back.
Both the dump engine (issue #2) and the load engine (issue #3) must go
through this module so the layout can never drift apart.

Layout (zip-relative):

    manifest.json
    dimensions/<name>.json                          REST /Dimensions entity, hierarchies expanded
    cubes/<name>.json                               REST /Cubes entity incl. Rules text
    views/<cube>/<name>.json                        public views
    subsets/<dimension>/<hierarchy>/<name>.json     public subsets
    processes/<name>.json                           REST /Processes entity
    chores/<name>.json
    data/<cube>.csv                                 header lines # cube,# dimensions; rows <e1>,...,<eN>,<value>
    security/users.json, groups.json, client_groups.json, permissions.json

Name and parent segments are percent-encoded with
``urllib.parse.quote(segment, safe="")`` so object names containing ``/``
or other special characters stay a single path segment.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Sequence
from typing import NamedTuple

TYPE_DIMENSIONS = "dimensions"
TYPE_CUBES = "cubes"
TYPE_VIEWS = "views"
TYPE_SUBSETS = "subsets"
TYPE_PROCESSES = "processes"
TYPE_CHORES = "chores"
TYPE_DATA = "data"
TYPE_SECURITY = "security"

OBJECT_TYPES: tuple[str, ...] = (
    TYPE_DIMENSIONS,
    TYPE_CUBES,
    TYPE_VIEWS,
    TYPE_SUBSETS,
    TYPE_PROCESSES,
    TYPE_CHORES,
    TYPE_DATA,
    TYPE_SECURITY,
)

MANIFEST_NAME = "manifest.json"

#: Fixed file names (without extension) stored under ``security/``.
SECURITY_FILE_NAMES: tuple[str, ...] = ("users", "groups", "client_groups", "permissions")

JSON_EXT = ".json"
DATA_EXT = ".csv"

#: Parent directory segments required between the type directory and the file.
PARENT_DEPTH: dict[str, int] = {TYPE_VIEWS: 1, TYPE_SUBSETS: 2}

_EXTENSION: dict[str, str] = {
    object_type: (DATA_EXT if object_type == TYPE_DATA else JSON_EXT) for object_type in OBJECT_TYPES
}


class ResolvedPath(NamedTuple):
    """One object location resolved from a zip path."""

    object_type: str
    name: str
    parents: tuple[str, ...]


def _quote_segment(segment: str) -> str:
    """Percent-encode one path segment (encodes ``/`` and other specials)."""
    return urllib.parse.quote(segment, safe="")


def _unquote_segment(segment: str) -> str:
    """Decode one percent-encoded path segment."""
    return urllib.parse.unquote(segment)


def _parent_hint(object_type: str) -> str:
    """Return a usage hint for the ``parent`` argument of one type."""
    if object_type == TYPE_VIEWS:
        return 'parent="CubeName"'
    if object_type == TYPE_SUBSETS:
        return 'parent=("DimensionName", "HierarchyName")'
    return "parent=None"


def _check_parents(object_type: str, parent: str | Sequence[str] | None) -> tuple[str, ...]:
    """Normalize ``parent`` to a tuple and enforce the depth ``object_type`` requires."""
    if parent is None:
        parents: tuple[str, ...] = ()
    elif isinstance(parent, str):
        parents = (parent,)
    else:
        parents = tuple(parent)
    expected_depth = PARENT_DEPTH.get(object_type, 0)
    if len(parents) != expected_depth:
        raise ValueError(
            f"{object_type} expects {expected_depth} parent segment(s) "
            f"({_parent_hint(object_type)}), got {len(parents)}"
        )
    for parent_segment in parents:
        if not parent_segment:
            raise ValueError("parent segment must not be empty")
    return parents


def build_path(object_type: str, name: str, parent: str | Sequence[str] | None = None) -> str:
    """Build the zip-relative path for one object.

    ``parent`` carries the enclosing objects: the cube name for views, the
    ``(dimension, hierarchy)`` pair for subsets, and nothing for the
    single-directory types (dimensions, cubes, processes, chores, data,
    security).
    """
    if object_type not in OBJECT_TYPES:
        raise ValueError(f"unknown object type: {object_type!r}")
    if not name:
        raise ValueError("object name must not be empty")
    parents = _check_parents(object_type, parent)
    segments = [
        object_type,
        *(_quote_segment(segment) for segment in parents),
        _quote_segment(name) + _EXTENSION[object_type],
    ]
    return "/".join(segments)


def resolve_path(path: str) -> ResolvedPath:
    """Resolve a zip-relative path back to its object type, name and parents.

    The inverse of :func:`build_path`. Raises ``ValueError`` for anything
    that is not a laid-out object file (including ``manifest.json``).
    """
    segments = path.split("/")
    if any(not segment for segment in segments):
        raise ValueError(f"malformed zip path: {path!r}")
    object_type = segments[0]
    if object_type not in OBJECT_TYPES:
        raise ValueError(f"not a known object directory: {path!r}")
    expected_depth = PARENT_DEPTH.get(object_type, 0)
    if len(segments) != expected_depth + 2:  # type dir + parents + file segment
        raise ValueError(f"unexpected segment count for {object_type}: {path!r}")
    extension = _EXTENSION[object_type]
    file_segment = segments[-1]
    if not file_segment.endswith(extension):
        raise ValueError(f"expected a {extension} file: {path!r}")
    parents = tuple(_unquote_segment(segment) for segment in segments[1:-1])
    name = _unquote_segment(file_segment[: -len(extension)])
    if not name:
        raise ValueError(f"empty object name: {path!r}")
    return ResolvedPath(object_type=object_type, name=name, parents=parents)

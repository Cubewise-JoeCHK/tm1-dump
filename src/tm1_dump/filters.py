"""Shared ``--include``/``--exclude`` filter parsing and matching.

Both engines (dump and load) go through this module so cherry-picking
means the same thing on both sides: a filter entry is ``TYPE=PATTERN``
(TYPE one of :data:`tm1_dump.zipio.OBJECT_TYPES`) or a bare ``PATTERN``
matching the object name in every type. Matching is case-insensitive
fnmatch, exclude wins over include, and a type with no include patterns
includes everything.
"""

from __future__ import annotations

import fnmatch

from tm1_dump import zipio

#: Filter entry type that matches every object type (bare ``PATTERN`` form).
ALL_TYPES = "*"


def parse_filters(include: list[str] | None, exclude: list[str] | None) -> dict[str, dict[str, list[str]]]:
    """Parse repeated ``--include``/``--exclude`` values into per-type patterns.

    Each entry is ``TYPE=PATTERN`` (TYPE must be one of
    :data:`tm1_dump.zipio.OBJECT_TYPES`) or a bare ``PATTERN`` that applies to
    every type. Raises ``ValueError`` for an unknown type.
    """
    return {"include": _parse_filter_entries(include), "exclude": _parse_filter_entries(exclude)}


def _parse_filter_entries(entries: list[str] | None) -> dict[str, list[str]]:
    """Split filter entries into ``{type or ALL_TYPES: [patterns]}``, validating types."""
    parsed: dict[str, list[str]] = {object_type: [] for object_type in zipio.OBJECT_TYPES}
    parsed[ALL_TYPES] = []
    for entry in entries or []:
        if "=" in entry:
            object_type, pattern = entry.split("=", 1)
            object_type = object_type.strip().lower()
            if object_type not in zipio.OBJECT_TYPES:
                valid = "|".join(zipio.OBJECT_TYPES)
                raise ValueError(f"unknown object type in filter {entry!r}: {object_type!r} (valid: {valid})")
        else:
            object_type, pattern = ALL_TYPES, entry
        pattern = pattern.strip()
        if pattern:
            parsed[object_type].append(pattern)
    return parsed


def object_allowed(name: str, filters: dict[str, dict[str, list[str]]], object_type: str) -> bool:
    """Decide whether ``name`` of ``object_type`` passes the include/exclude filters.

    Matching is case-insensitive fnmatch; exclude wins over include; a type
    with no include patterns includes everything.
    """
    include_patterns = filters["include"].get(object_type, []) + filters["include"].get(ALL_TYPES, [])
    exclude_patterns = filters["exclude"].get(object_type, []) + filters["exclude"].get(ALL_TYPES, [])
    lowered = name.lower()
    if include_patterns and not any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in include_patterns):
        return False
    if any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in exclude_patterns):
        return False
    return True

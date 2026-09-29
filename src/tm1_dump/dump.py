"""Dump engine: write one TM1 instance into one zip archive.

Discovers every object on the server, applies the ``--include``/``--exclude``
filters per type, exports objects in parallel (``--workers`` threads) and
writes everything into one zip: object JSON per the :mod:`tm1_dump.zipio`
layout, cube data as CSV (``--no-data`` keeps only the ``}ElementAttributes_*``
attribute values), security as four JSON files, plus a
``manifest.json`` index with counts, sha256 digests and export errors.

One object failing never aborts the batch: it is logged to stderr, recorded
in the manifest under ``errors`` and the dump continues; the exit code is 1
at the end if anything failed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

import TM1py
from TM1py import TM1Service

from tm1_dump import zipio
from tm1_dump.config import DEFAULT_SSL, ConnectionConfig, connection_problem, resolve_connection
from tm1_dump.filters import object_allowed, parse_filters
from tm1_dump.manifest import Manifest, ManifestObject, SourceInfo

DEFAULT_WORKERS = 8

#: Control cubes holding per-group object rights -> section in permissions.json.
RIGHTS_CUBES: dict[str, str] = {
    "}CubeSecurity": "cubes",
    "}DimensionSecurity": "dimensions",
    "}ProcessSecurity": "processes",
    "}ChoreSecurity": "chores",
}

GROUP_DIMENSION_NAME = "}groups"  # compared case-insensitively

#: Name prefix of the control cubes carrying element-attribute values; the
#: only data ``--no-data`` still exports (compared case-insensitively).
ELEMENT_ATTRIBUTES_PREFIX = "}elementattributes_"

#: One export record: ``(object_type, name, parent, file_content)``.
ExportRecord = tuple[str, str, "str | Sequence[str] | None", str]
#: One job = ``(label, thunk)``; ``thunk()`` returns records and may raise.
Job = tuple[tuple[str, str], Callable[[], list[ExportRecord]]]


# --------------------------------------------------------------------------- connection


def _service_kwargs(conn: ConnectionConfig) -> dict:
    """Build TM1Service keyword arguments from resolved connection settings.

    SSL defaults to ON (the TM1 server default); only explicitly set
    settings are forwarded.
    """
    kwargs: dict = {"ssl": conn.ssl if conn.ssl is not None else DEFAULT_SSL}
    for setting in ("address", "port", "user", "password", "namespace"):
        value = getattr(conn, setting)
        if value is not None:
            kwargs[setting] = value
    return kwargs


def _connect(conn: ConnectionConfig) -> TM1Service:
    """Open the TM1 connection."""
    return TM1Service(**_service_kwargs(conn))


# --------------------------------------------------------------------------- zip sink


class _ZipSink:
    """Thread-safe zip writer recording manifest entries and export errors."""

    def __init__(self, zip_file: zipfile.ZipFile) -> None:
        self._zip_file = zip_file
        self._lock = threading.Lock()
        self.objects: list[ManifestObject] = []
        self.errors: list[dict[str, str]] = []

    def write(self, object_type: str, name: str, content: str, parent: str | Sequence[str] | None = None) -> None:
        """Write one object file into the zip and index it in the manifest."""
        path = zipio.build_path(object_type, name, parent)
        data = content.encode("utf-8")
        with self._lock:
            self._zip_file.writestr(path, data)
            self.objects.append(ManifestObject(object_type, name, path, hashlib.sha256(data).hexdigest()))

    def record_error(self, object_type: str, name: str, error: Exception) -> None:
        """Record one failed object in the manifest error list."""
        with self._lock:
            self.errors.append({"type": object_type, "name": name, "error": str(error)})


# --------------------------------------------------------------------------- job runner


def _run_jobs(jobs: list[Job], workers: int, sink: _ZipSink) -> None:
    """Run export jobs, fanning out over a thread pool when ``workers`` > 1."""
    if not jobs:
        return
    if workers <= 1:
        for label, thunk in jobs:
            _run_one_job(label, thunk, sink)
        return
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        # _run_one_job never raises: every failure is caught and recorded.
        for _ in pool.map(lambda job: _run_one_job(job[0], job[1], sink), jobs):
            pass


def _run_one_job(label: tuple[str, str], thunk, sink: _ZipSink) -> None:
    """Run one export job; a failure is logged and recorded, never raised."""
    object_type, name = label
    try:
        for record_type, record_name, record_parent, content in thunk():
            sink.write(record_type, record_name, content, record_parent)
    except Exception as error:  # one object failing must not abort the batch
        print(f"tm1-dump: error exporting {object_type} {name!r}: {error}", file=sys.stderr)
        sink.record_error(object_type, name, error)


# --------------------------------------------------------------------------- job collectors


def _collect_dimension_jobs(tm1: TM1Service, filters: dict) -> list[Job]:
    """One job per dimension: the full entity with hierarchies expanded."""
    return [
        ((zipio.TYPE_DIMENSIONS, name), _dimension_thunk(tm1, name))
        for name in sorted(tm1.dimensions.get_all_names())
        if object_allowed(name, filters, zipio.TYPE_DIMENSIONS)
    ]


def _dimension_thunk(tm1: TM1Service, name: str) -> Callable[[], list[ExportRecord]]:
    """Build a thunk fetching one dimension with all hierarchies expanded."""

    def thunk() -> list[ExportRecord]:
        return [(zipio.TYPE_DIMENSIONS, name, None, _dimension_body(tm1.dimensions.get(name)))]

    return thunk


def _dimension_body(dimension) -> str:
    """Serialize one dimension entity including its element attributes.

    TM1py's ``Dimension.body`` omits ``ElementAttributes`` on every
    hierarchy (attributes could not be created in one batch on old TM1
    versions, so TM1py drops them on re-serialization); they are
    re-attached here so they survive the zip.
    """
    body = json.loads(dimension.body)
    for hierarchy, hierarchy_body in zip(dimension.hierarchies, body.get("Hierarchies", []), strict=False):
        attributes = [
            {"Name": attribute.name, "Type": str(attribute.attribute_type)}
            for attribute in hierarchy.element_attributes
        ]
        if attributes:
            hierarchy_body["ElementAttributes"] = attributes
    return json.dumps(body)


def _object_body_thunk(object_type: str, tm1_object) -> Callable[[], list[ExportRecord]]:
    """Build a thunk serializing a fetched TM1py object body under its own name."""

    def thunk() -> list[ExportRecord]:
        return [(object_type, tm1_object.name, None, tm1_object.body)]

    return thunk


def _collect_cube_jobs(cubes: list, filters: dict) -> list[Job]:
    """One job per cube: the REST entity including its Rules text."""
    return [
        ((zipio.TYPE_CUBES, cube.name), _object_body_thunk(zipio.TYPE_CUBES, cube))
        for cube in cubes
        if object_allowed(cube.name, filters, zipio.TYPE_CUBES)
    ]


def _collect_view_jobs(tm1: TM1Service, filters: dict, cube_names: list[str]) -> list[Job]:
    """One job per cube exporting its public views (filtered per view name)."""
    return [((zipio.TYPE_VIEWS, cube_name), _views_thunk(tm1, cube_name, filters)) for cube_name in cube_names]


def _views_thunk(tm1: TM1Service, cube_name: str, filters: dict):
    """Fetch the cube's views and return records for the allowed public ones."""

    def thunk() -> list[ExportRecord]:
        # tm1py returns (private_views, public_views) - the public list is second.
        _private_views, public_views = tm1.views.get_all(cube_name)
        return [
            (zipio.TYPE_VIEWS, view.name, cube_name, view.body)
            for view in public_views
            if object_allowed(view.name, filters, zipio.TYPE_VIEWS)
        ]

    return thunk


def _collect_subset_jobs(tm1: TM1Service, filters: dict, dimension_names: list[str]) -> list[Job]:
    """One job per dimension exporting its public subsets (filtered per subset name)."""
    return [
        ((zipio.TYPE_SUBSETS, dimension_name), _subsets_thunk(tm1, dimension_name, filters))
        for dimension_name in dimension_names
    ]


def _subsets_thunk(tm1: TM1Service, dimension_name: str, filters: dict):
    """Walk the dimension's hierarchies and export every allowed public subset."""

    def thunk() -> list[ExportRecord]:
        records: list[ExportRecord] = []
        for hierarchy_name in tm1.hierarchies.get_all_names(dimension_name):
            for subset_name in tm1.subsets.get_all_names(dimension_name, hierarchy_name):
                if not object_allowed(subset_name, filters, zipio.TYPE_SUBSETS):
                    continue
                subset = tm1.subsets.get(subset_name, dimension_name, hierarchy_name)
                records.append((zipio.TYPE_SUBSETS, subset_name, (dimension_name, hierarchy_name), subset.body))
        return records

    return thunk


def _collect_typed_object_jobs(objects: list, object_type: str, filters: dict) -> list[Job]:
    """One job per fetched object (processes, chores) filtered by name."""
    return [
        ((object_type, tm1_object.name), _object_body_thunk(object_type, tm1_object))
        for tm1_object in objects
        if object_allowed(tm1_object.name, filters, object_type)
    ]


def _collect_security_jobs(tm1: TM1Service, filters: dict) -> list[Job]:
    """The four security files: users, groups, client_groups, permissions."""
    thunks = {
        "users": _users_thunk,
        "groups": _groups_thunk,
        "client_groups": _client_groups_thunk,
        "permissions": _permissions_thunk,
    }
    return [
        ((zipio.TYPE_SECURITY, file_stem), thunk(tm1))
        for file_stem, thunk in thunks.items()
        if object_allowed(file_stem, filters, zipio.TYPE_SECURITY)
    ]


def _users_thunk(tm1: TM1Service):
    """Export all users (REST never returns passwords) as a JSON array."""

    def thunk() -> list[ExportRecord]:
        users = sorted(
            (json.loads(user.body) for user in tm1.security.get_all_users()),
            key=lambda entry: str(entry.get("Name", "")).casefold(),
        )
        return [(zipio.TYPE_SECURITY, "users", None, json.dumps(users, indent=2))]

    return thunk


def _groups_thunk(tm1: TM1Service):
    """Export all group names as a sorted JSON array."""

    def thunk() -> list[ExportRecord]:
        groups = sorted(tm1.security.get_all_groups(), key=str.casefold)
        return [(zipio.TYPE_SECURITY, "groups", None, json.dumps(groups, indent=2))]

    return thunk


def _client_groups_thunk(tm1: TM1Service):
    """Export user -> sorted group memberships as a JSON object."""

    def thunk() -> list[ExportRecord]:
        memberships = {
            user.name: sorted(tm1.security.get_groups(user.name), key=str.casefold)
            for user in tm1.security.get_all_users()
        }
        return [(zipio.TYPE_SECURITY, "client_groups", None, json.dumps(memberships, indent=2, sort_keys=True))]

    return thunk


def _permissions_thunk(tm1: TM1Service):
    """Export per-group object rights from the security control cubes."""

    def thunk() -> list[ExportRecord]:
        # All four sections always exist; a missing rights cube leaves an empty one.
        permissions: dict[str, dict] = {section: {} for section in RIGHTS_CUBES.values()}
        for rights_cube, section in RIGHTS_CUBES.items():
            try:
                permissions[section] = _read_rights_cube(tm1, rights_cube)
            except Exception as error:
                # A missing rights cube is not a dump failure; note it and move on.
                print(f"tm1-dump: warning: skipping rights cube {rights_cube!r}: {error}", file=sys.stderr)
        return [(zipio.TYPE_SECURITY, "permissions", None, json.dumps(permissions, indent=2, sort_keys=True))]

    return thunk


def _read_rights_cube(tm1: TM1Service, rights_cube: str) -> dict[str, dict[str, str]]:
    """Read one rights control cube into ``{object: {group: right}}``."""
    dimension_names = tm1.cubes.get_dimension_names(rights_cube)
    raw = tm1.cells.execute_mdx_csv(_members_crossjoin_mdx(rights_cube, dimension_names), skip_zeros=True)
    rows = [row for row in csv.reader(io.StringIO(raw)) if row]
    if len(rows) < 2:
        return {}
    header = rows[0]
    coord_count = len(header) - 1  # the last CSV column is the cell value
    group_index = next((i for i, name in enumerate(header) if str(name).strip().lower() == GROUP_DIMENSION_NAME), 1)
    assignments: dict[str, dict[str, str]] = {}
    for row in rows[1:]:
        value = row[-1]
        if not value.strip():
            continue
        object_name = ", ".join(row[i] for i in range(coord_count) if i != group_index)
        assignments.setdefault(object_name, {})[row[group_index]] = value
    return assignments


def _collect_data_jobs(tm1: TM1Service, cubes: list, filters: dict, no_data: bool = False) -> list[Job]:
    """One job per cube (filtered by cube name) exporting its cells as CSV.

    With ``no_data`` the phase narrows to the ``}ElementAttributes_*``
    control cubes (case-insensitive) so a light dump keeps element-attribute
    values; the ``--include``/``--exclude`` filters still apply on top, so
    combining ``--no-data`` with ``--exclude "data=..."`` yields no data
    files at all.
    """
    return [
        ((zipio.TYPE_DATA, cube.name), _data_thunk(tm1, cube))
        for cube in cubes
        if (not no_data or cube.name.lower().startswith(ELEMENT_ATTRIBUTES_PREFIX))
        and object_allowed(cube.name, filters, zipio.TYPE_DATA)
    ]


def _data_thunk(tm1: TM1Service, cube):
    """Export one cube's non-empty cells in stable sorted order."""

    def thunk() -> list[ExportRecord]:
        return [(zipio.TYPE_DATA, cube.name, None, export_cube_data(tm1, cube.name, list(cube.dimensions)))]

    return thunk


def export_cube_data(tm1: TM1Service, cube_name: str, dimension_names: list[str]) -> str:
    """Export one cube as contract-format CSV: comment header, then sorted rows."""
    mdx = _members_crossjoin_mdx(cube_name, dimension_names)
    try:
        # Server-side blob route: faster on large cellsets, fewer server round-trips.
        raw = tm1.cells.execute_mdx_csv(mdx, skip_zeros=True, use_blob=True)
    except Exception as error:
        print(f"tm1-dump: blob export failed for cube {cube_name!r}, retrying without blob: {error}", file=sys.stderr)
        raw = tm1.cells.execute_mdx_csv(mdx, skip_zeros=True)
    lines = raw.splitlines()
    if lines:
        lines = lines[1:]  # drop the server column header (dimension names ..., Value)
    lines = sorted(lines)
    output = [f"# cube,{cube_name}", f"# dimensions,{','.join(dimension_names)}", *lines]
    return "\r\n".join(output) + "\r\n"


def _members_crossjoin_mdx(cube_name: str, dimension_names: list[str]) -> str:
    """Build an MDX selecting the cross product of all members of every dimension."""
    member_sets = [f"{{[{_mdx_escape(name)}].MEMBERS}}" for name in dimension_names]
    return f"SELECT NON EMPTY {' * '.join(member_sets)} ON 0 FROM [{_mdx_escape(cube_name)}]"


def _mdx_escape(name: str) -> str:
    """Escape a name for use inside MDX square brackets."""
    return name.replace("]", "]]")


# --------------------------------------------------------------------------- engine


def run_dump(args: argparse.Namespace) -> int:
    """Dump one TM1 instance into the zip given by ``args.out``."""
    started = time.monotonic()
    try:
        filters = parse_filters(args.include, args.exclude)
    except ValueError as error:
        print(f"tm1-dump: error: {error}", file=sys.stderr)
        return 2

    conn = resolve_connection(args)
    problem = connection_problem(conn)
    if problem:
        print(f"tm1-dump: {problem}", file=sys.stderr)
        return 2
    try:
        tm1 = _connect(conn)
    except Exception as error:
        print(f"tm1-dump: error: cannot connect to TM1: {error}", file=sys.stderr)
        return 2

    try:
        return _dump_to_zip(tm1, conn, filters, args, started)
    finally:
        try:
            tm1.logout()
        except Exception:
            pass


def _dump_to_zip(tm1: TM1Service, conn: ConnectionConfig, filters: dict, args: argparse.Namespace, started: float) -> int:
    """Discover, export and write the dump zip; return the process exit code."""
    workers = args.workers if args.workers and args.workers > 0 else DEFAULT_WORKERS
    server = tm1.server.get_server_name()
    out_path = args.out or f"{_filename_safe(server)}_{datetime.now():%Y%m%d_%H%M%S}.zip"
    tmp_path = out_path + ".tmp"

    dimension_names = sorted(tm1.dimensions.get_all_names())
    cubes = tm1.cubes.get_all()

    manifest = Manifest(
        tool_version=_tool_version(),
        source=SourceInfo(server=server, address=conn.address or "", port=conn.port or 0, tm1py_version=TM1py.__version__),
        filters=filters,
    )

    phases: list[tuple[str, list[Job]]] = [
        (zipio.TYPE_DIMENSIONS, _collect_dimension_jobs(tm1, filters)),
        (zipio.TYPE_CUBES, _collect_cube_jobs(cubes, filters)),
        (zipio.TYPE_VIEWS, _collect_view_jobs(tm1, filters, [cube.name for cube in cubes])),
        (zipio.TYPE_SUBSETS, _collect_subset_jobs(tm1, filters, dimension_names)),
        (zipio.TYPE_PROCESSES, _collect_typed_object_jobs(tm1.processes.get_all(), zipio.TYPE_PROCESSES, filters)),
        (zipio.TYPE_CHORES, _collect_typed_object_jobs(tm1.chores.get_all(), zipio.TYPE_CHORES, filters)),
        (zipio.TYPE_SECURITY, _collect_security_jobs(tm1, filters)),
        (zipio.TYPE_DATA, _collect_data_jobs(tm1, cubes, filters, no_data=args.no_data)),
    ]

    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        sink = _ZipSink(zip_file)
        for object_type, jobs in phases:
            print(f"==> {object_type}: exporting {len(jobs)} job(s) with {workers} worker(s)", file=sys.stderr)
            _run_jobs(jobs, workers, sink)
            exported = sum(1 for entry in sink.objects if entry.type == object_type)
            failed = sum(1 for error in sink.errors if error["type"] == object_type)
            print(f"==> {object_type}: wrote {exported}, {failed} error(s)", file=sys.stderr)
        manifest.objects = sorted(sink.objects, key=lambda entry: (entry.type, entry.file))
        manifest.errors = sink.errors
        manifest.counts = {
            object_type: sum(1 for entry in manifest.objects if entry.type == object_type)
            for object_type in zipio.OBJECT_TYPES
        }
        zip_file.writestr(zipio.MANIFEST_NAME, manifest.to_json())

    os.replace(tmp_path, out_path)  # atomic once the zip is complete

    elapsed = time.monotonic() - started
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"wrote {out_path} ({len(manifest.objects)} objects, {size_mb:.1f} MB, {elapsed:.1f}s)")
    return 1 if manifest.errors else 0


def _filename_safe(name: str) -> str:
    """Reduce a server name to characters that are safe in a file name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "tm1"


def _tool_version() -> str:
    """Return the installed tm1-dump version."""
    try:
        return package_version("tm1-dump")
    except PackageNotFoundError:
        return "0.0.0+unknown"

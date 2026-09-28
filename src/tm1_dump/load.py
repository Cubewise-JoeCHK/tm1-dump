"""Load engine: reload a tm1-dump zip onto a TM1 server.

The CLI dispatches here via :func:`run_load`. The zip is read directly
(no extraction) and every file's sha256 is verified against the manifest
before the server is touched. Objects reload in dependency order —
dimensions, cubes (+ rules), views, subsets, processes, chores, data,
security — parallel within each type, sequential across types.

Shared zip-side contract (with the dump engine, issue #2):

- ``data/<cube>.csv`` starts with ``# cube,<name>`` and
  ``# dimensions,<d1>,...`` header lines, followed by one row per cell
  ``<e1>,...,<eN>,<value>``, written as RFC-4180 CSV (fields containing
  commas must be quoted) so names and values with commas survive.
- ``security/users.json`` is a list of TM1 ``/Users`` entity bodies,
  ``security/groups.json`` a list of group names (or ``{"Name": ...}``
  entities), ``security/client_groups.json`` a list of
  ``{"client": ..., "groups": [...]}`` records, and
  ``security/permissions.json`` a list of ``{"object_type", "object",
  "group", "permission"}`` records where ``object_type`` is one of
  ``cubes|dimensions|processes|chores`` and ``permission`` one of
  ``NONE|READ|WRITE|RESERVE|LOCK|ADMIN`` (any case).

Group rights are replayed into the TM1 global-security control cubes
(``}CubeSecurity``, ``}DimensionSecurity``, ``}ProcessSecurity``,
``}ChoreSecurity``), one (group, object) cell per record.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import secrets
import sys
import zipfile
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, NamedTuple

from TM1py.Objects import Chore, Cube, Dimension, MDXView, NativeView, Process, Subset, User
from TM1py.Services import TM1Service

from tm1_dump import zipio
from tm1_dump.config import ConnectionConfig, resolve_connection
from tm1_dump.manifest import Manifest, ManifestObject

DEFAULT_WORKERS = 8
DATA_CHUNK_ROWS = 10_000
RANDOM_PASSWORD_CHARS = 16

#: Phases run sequentially, in dependency order.
LOAD_ORDER: tuple[str, ...] = (
    zipio.TYPE_DIMENSIONS,
    zipio.TYPE_CUBES,
    zipio.TYPE_VIEWS,
    zipio.TYPE_SUBSETS,
    zipio.TYPE_PROCESSES,
    zipio.TYPE_CHORES,
    zipio.TYPE_DATA,
    zipio.TYPE_SECURITY,
)

#: Object types dropped by ``--clean``, children before parents. Deleting a
#: cube removes its views and data, deleting a dimension removes its subsets,
#: so views/subsets/data need no separate deletion pass. Security objects are
#: never deleted (only overwritten) so loading cannot lock the operator out.
CLEAN_ORDER: tuple[str, ...] = (
    zipio.TYPE_CHORES,
    zipio.TYPE_PROCESSES,
    zipio.TYPE_CUBES,
    zipio.TYPE_DIMENSIONS,
)

#: TM1 global-security control cubes holding group rights, per object type.
PERMISSION_CONTROL_CUBES: dict[str, str] = {
    zipio.TYPE_CUBES: "}CubeSecurity",
    zipio.TYPE_DIMENSIONS: "}DimensionSecurity",
    zipio.TYPE_PROCESSES: "}ProcessSecurity",
    zipio.TYPE_CHORES: "}ChoreSecurity",
}

#: Rights accepted in permissions.json, normalized to upper case.
VALID_PERMISSIONS: tuple[str, ...] = ("NONE", "READ", "WRITE", "RESERVE", "LOCK", "ADMIN")

CHORES_DST_NOTICE = (
    "chore start times are applied in the target server's local timezone; "
    "across a DST change the absolute time can shift by an hour"
)


class _Failure(NamedTuple):
    """One object that failed to load, for the end-of-run summary."""

    object_type: str
    name: str
    error: str


@dataclass
class _Entry:
    """One manifest object plus the parents its zip path carries."""

    spec: ManifestObject
    parents: tuple[str, ...]

    @property
    def object_type(self) -> str:
        return self.spec.type

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def file(self) -> str:
        return self.spec.file


def run_load(args: argparse.Namespace) -> int:
    """Load the dump zip ``args.zip_file`` onto the target server.

    Returns 0 when everything loaded, 1 when anything failed (validation,
    connection, or any individual object — the rest still loads, and a
    failure summary is printed to stderr at the end).
    """
    try:
        archive = zipfile.ZipFile(args.zip_file)
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"tm1-dump: cannot open zip {args.zip_file!r}: {exc}", file=sys.stderr)
        return 1

    with archive:
        manifest = _read_manifest(archive, args.zip_file)
        if manifest is None:
            return 1
        problems = _verify_archive(archive, manifest)
        if problems:
            print(f"tm1-dump: refusing to load {args.zip_file!r} — manifest verification failed:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1

        resolved = resolve_connection(args)
        problem = _connection_problem(resolved)
        if problem:
            print(f"tm1-dump: {problem}", file=sys.stderr)
            return 1

        entries_by_type = _group_entries(manifest)
        workers = args.workers or DEFAULT_WORKERS
        try:
            tm1 = _connect(resolved, workers)
        except Exception as exc:
            print(
                f"tm1-dump: cannot connect to {resolved.address}:{resolved.port}: {_error_text(exc)}",
                file=sys.stderr,
            )
            return 1

        with tm1:
            if args.dry_run:
                _print_plan(tm1, archive, args.zip_file, entries_by_type)
                return 0
            return _load_all(tm1, archive, entries_by_type, workers, clean=args.clean)


def _read_manifest(archive: zipfile.ZipFile, zip_path: str) -> Manifest | None:
    """Parse manifest.json; print a clear error and return None when invalid."""
    try:
        text = archive.read(zipio.MANIFEST_NAME).decode("utf-8")
        return Manifest.from_json(text)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"tm1-dump: invalid {zipio.MANIFEST_NAME} in {zip_path!r}: {exc}", file=sys.stderr)
        return None


def _verify_archive(archive: zipfile.ZipFile, manifest: Manifest) -> list[str]:
    """Return every integrity problem: files missing from the zip, sha256
    mismatches, or zip paths whose layout contradicts the manifest."""
    problems: list[str] = []
    names = set(archive.namelist())
    for spec in manifest.objects:
        if spec.file not in names:
            problems.append(f"listed in manifest but missing from zip: {spec.file}")
            continue
        digest = hashlib.sha256(archive.read(spec.file)).hexdigest()
        if digest != spec.sha256.lower():
            problems.append(f"sha256 mismatch for {spec.file} (zip content does not match the manifest)")
            continue
        try:
            resolved = zipio.resolve_path(spec.file)
        except ValueError as exc:
            problems.append(f"{spec.file} violates the zip layout contract: {exc}")
            continue
        if resolved.object_type != spec.type or resolved.name != spec.name:
            problems.append(
                f"manifest says {spec.type} {spec.name!r} but {spec.file} "
                f"resolves to {resolved.object_type} {resolved.name!r}"
            )
    return problems


def _connection_problem(resolved: ConnectionConfig) -> str | None:
    """Return why the resolved connection cannot be used, or None."""
    missing = [
        flag
        for flag, value in (
            ("--address / TM1_ADDRESS", resolved.address),
            ("--port / TM1_PORT", resolved.port),
            ("--user / TM1_USER", resolved.user),
        )
        if value is None
    ]
    if missing:
        return (
            "no target server: missing "
            + ", ".join(missing)
            + " (pass CLI flags, set env vars, or use --config-file)"
        )
    return None


def _connect(resolved: ConnectionConfig, workers: int) -> TM1Service:
    """Open the TM1Service; SSL defaults to on unless explicitly disabled."""
    return TM1Service(
        address=resolved.address,
        port=resolved.port,
        user=resolved.user,
        password=resolved.password,
        ssl=True if resolved.ssl is None else resolved.ssl,
        namespace=resolved.namespace,
        connection_pool_size=max(10, workers + 2),
    )


def _group_entries(manifest: Manifest) -> dict[str, list[_Entry]]:
    """Group manifest objects by type, resolving each file's layout parents."""
    grouped: dict[str, list[_Entry]] = {}
    for spec in manifest.objects:
        parents = zipio.resolve_path(spec.file).parents
        grouped.setdefault(spec.type, []).append(_Entry(spec=spec, parents=parents))
    return grouped


def _load_all(
    tm1: TM1Service,
    archive: zipfile.ZipFile,
    entries_by_type: dict[str, list[_Entry]],
    workers: int,
    clean: bool,
) -> int:
    """Clean (optional) and load every phase; return the process exit code."""
    failures: list[_Failure] = []
    if clean:
        dropped = _clean_matching(tm1, entries_by_type, failures)
        print(f"clean: dropped {dropped} matching object(s)")

    for object_type in LOAD_ORDER:
        entries = entries_by_type.get(object_type, [])
        if not entries:
            continue
        if object_type == zipio.TYPE_DATA:
            _load_data(tm1, archive, entries, workers, failures)
        elif object_type == zipio.TYPE_SECURITY:
            for notice in _load_security(tm1, archive, entries, failures):
                print(f"notice: {notice}")
        else:
            _load_model_type(tm1, archive, entries, workers, failures)
            if object_type == zipio.TYPE_CHORES:
                print(f"notice: {CHORES_DST_NOTICE}")
        print(f"{object_type}: done ({len(entries)} object(s))")

    _report_failures(failures)
    return 1 if failures else 0


def _clean_matching(
    tm1: TM1Service,
    entries_by_type: dict[str, list[_Entry]],
    failures: list[_Failure],
) -> int:
    """``--clean``: drop matching objects children-first, return how many."""
    delete_calls: dict[str, Callable[[TM1Service, str], Any]] = {
        zipio.TYPE_CHORES: lambda tm1_ref, name: tm1_ref.chores.delete(name),
        zipio.TYPE_PROCESSES: lambda tm1_ref, name: tm1_ref.processes.delete(name),
        zipio.TYPE_CUBES: lambda tm1_ref, name: tm1_ref.cubes.delete(name),
        zipio.TYPE_DIMENSIONS: lambda tm1_ref, name: tm1_ref.dimensions.delete(name),
    }
    dropped = 0
    for object_type in CLEAN_ORDER:
        for entry in entries_by_type.get(object_type, ()):
            try:
                if _object_exists(tm1, entry):
                    delete_calls[object_type](tm1, entry.name)
                    print(f"  clean: dropped {object_type[:-1]} {entry.name!r}")
                    dropped += 1
            except Exception as exc:
                failures.append(_Failure(object_type, entry.name, _error_text(exc)))
    return dropped


def _object_exists(tm1: TM1Service, entry: _Entry) -> bool:
    """Return whether the object already exists on the target (public only)."""
    if entry.object_type == zipio.TYPE_DIMENSIONS:
        return bool(tm1.dimensions.exists(entry.name))
    if entry.object_type == zipio.TYPE_CUBES:
        return bool(tm1.cubes.exists(entry.name))
    if entry.object_type == zipio.TYPE_VIEWS:
        return bool(tm1.views.exists(entry.parents[0], entry.name, private=False))
    if entry.object_type == zipio.TYPE_SUBSETS:
        return bool(tm1.subsets.exists(entry.name, entry.parents[0], hierarchy_name=entry.parents[1], private=False))
    if entry.object_type == zipio.TYPE_PROCESSES:
        return bool(tm1.processes.exists(entry.name))
    if entry.object_type == zipio.TYPE_CHORES:
        return bool(tm1.chores.exists(entry.name))
    raise ValueError(f"no existence check for object type {entry.object_type!r}")


def _load_model_type(
    tm1: TM1Service,
    archive: zipfile.ZipFile,
    entries: list[_Entry],
    workers: int,
    failures: list[_Failure],
) -> int:
    """Create-or-overwrite every object of one dimensions..chores type."""

    def load_one(entry: _Entry) -> None:
        text = archive.read(entry.file).decode("utf-8")
        _upsert_object(tm1, entry, text)

    return _run_parallel(load_one, entries, workers, failures)


def _run_parallel(
    task: Callable[[_Entry], None],
    entries: list[_Entry],
    workers: int,
    failures: list[_Failure],
) -> int:
    """Run task for every entry in parallel; isolate per-object failures."""
    if not entries:
        return 0
    loaded = 0
    with ThreadPoolExecutor(max_workers=min(workers, len(entries))) as executor:
        futures = [(entry, executor.submit(task, entry)) for entry in entries]
        for entry, future in futures:
            try:
                future.result()
                loaded += 1
            except Exception as exc:
                failures.append(_Failure(entry.object_type, entry.name, _error_text(exc)))
    return loaded


def _bind_name(bind: str) -> str:
    """Extract the entity name from an odata bind like ``Dimensions('P&L')``."""
    start = bind.rfind("('") + 2
    end = bind.rfind("')")
    if start < 2 or end < start:
        raise ValueError(f"cannot read an entity name from odata bind {bind!r}")
    return bind[start:end]


def _parse_cube(text: str) -> Cube:
    """Build a Cube from a dump JSON body.

    Accepts both the TM1py ``.body`` shape (``Dimensions@odata.bind``) and
    the expanded REST entity (``Dimensions: [{"Name": ...}]``).
    """
    body = json.loads(text)
    if "Dimensions@odata.bind" in body:
        dimensions = [_bind_name(bind) for bind in body["Dimensions@odata.bind"]]
    else:
        dimensions = [dimension["Name"] for dimension in body.get("Dimensions", [])]
    return Cube(name=body["Name"], dimensions=dimensions, rules=body.get("Rules") or None)


def _parse_subset(text: str) -> Subset:
    """Build a Subset from a dump JSON body.

    Accepts the TM1py ``.body`` shape (``Hierarchy@odata.bind``,
    ``Elements@odata.bind``) and the expanded REST entity (``UniqueName``,
    ``Hierarchy``, ``Elements``).
    """
    body = json.loads(text)
    alias = body.get("Alias")
    expression = body.get("Expression")
    dimension_name, hierarchy_name = _subset_parents(body)
    elements = None
    if not expression:
        if "Elements@odata.bind" in body:
            elements = [_bind_name(bind) for bind in body["Elements@odata.bind"]]
        else:
            elements = [element["Name"] for element in body.get("Elements", [])]
    return Subset(
        subset_name=body["Name"],
        dimension_name=dimension_name,
        hierarchy_name=hierarchy_name,
        alias=alias,
        expression=expression,
        elements=elements,
    )


def _subset_parents(body: dict) -> tuple[str, str]:
    """Resolve (dimension, hierarchy) from any of the three body shapes."""
    if "Hierarchy@odata.bind" in body:
        dimension_bind, hierarchy_bind = body["Hierarchy@odata.bind"].split("/")
        return _bind_name(dimension_bind), _bind_name(hierarchy_bind)
    if "UniqueName" in body:  # "[Dim].[Hierarchy].[Subset]"
        parts = body["UniqueName"].strip("[]").split("].[")
        return parts[0], parts[1]
    hierarchy = body.get("Hierarchy") or {}
    dimension = hierarchy.get("Dimension") or {}
    return dimension.get("Name", ""), hierarchy.get("Name", "")


def _parse_object(entry: _Entry, text: str) -> Any:
    """Materialize the TM1py object stored in one zip file."""
    if entry.object_type == zipio.TYPE_DIMENSIONS:
        return Dimension.from_json(text)
    if entry.object_type == zipio.TYPE_CUBES:
        return _parse_cube(text)
    if entry.object_type == zipio.TYPE_VIEWS:
        cube = entry.parents[0]
        if "MDX" in json.loads(text):
            return MDXView.from_json(text, cube_name=cube)
        return NativeView.from_json(text, cube_name=cube)
    if entry.object_type == zipio.TYPE_SUBSETS:
        return _parse_subset(text)
    if entry.object_type == zipio.TYPE_PROCESSES:
        return Process.from_json(text)
    if entry.object_type == zipio.TYPE_CHORES:
        return Chore.from_json(text)
    raise ValueError(f"cannot parse object type {entry.object_type!r}")


def _upsert_object(tm1: TM1Service, entry: _Entry, text: str) -> None:
    """Create or overwrite one dimensions/cubes/views/subsets/processes/chores
    object via TM1py's update-or-create (public objects only)."""
    tm1_object = _parse_object(entry, text)
    if entry.object_type == zipio.TYPE_DIMENSIONS:
        tm1.dimensions.update_or_create(tm1_object)
    elif entry.object_type == zipio.TYPE_CUBES:
        tm1.cubes.update_or_create(tm1_object)
        if tm1_object.rules is not None:
            tm1.cubes.update_or_create_rules(tm1_object.name, tm1_object.rules)
    elif entry.object_type == zipio.TYPE_VIEWS:
        tm1.views.update_or_create(tm1_object, private=False)
    elif entry.object_type == zipio.TYPE_SUBSETS:
        tm1.subsets.update_or_create(tm1_object, private=False)
    elif entry.object_type == zipio.TYPE_PROCESSES:
        tm1.processes.update_or_create(tm1_object)
    elif entry.object_type == zipio.TYPE_CHORES:
        tm1.chores.update_or_create(tm1_object)
    else:
        raise ValueError(f"cannot upsert object type {entry.object_type!r}")


def _coerce_data_value(raw: str) -> Any:
    """Numeric-looking CSV fields become numbers; anything else stays text."""
    text = raw.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return raw


def _iter_data_chunks(
    archive: zipfile.ZipFile,
    entry: _Entry,
) -> Iterator[tuple[dict[tuple[str, ...], Any], list[str]]]:
    """Yield (cellset, dimension_names) chunks from one data/<cube>.csv."""
    with archive.open(entry.file) as raw_handle:
        text = io.TextIOWrapper(raw_handle, encoding="utf-8-sig", newline="")
        dimensions: list[str] = []
        chunk: dict[tuple[str, ...], Any] = {}
        for row in csv.reader(text):
            if not row:
                continue
            if row[0].startswith("#"):
                header = row[0].lstrip("#").strip().lower()
                if header == "dimensions":
                    dimensions = [name.strip() for name in row[1:]]
                elif header != "cube":  # "# cube,<name>" is informational
                    raise ValueError(f"unknown header line {row!r}")
                continue
            if not dimensions:
                raise ValueError("missing '# dimensions' header line")
            if len(row) != len(dimensions) + 1:
                raise ValueError(
                    f"row {row!r} has {len(row)} fields, expected {len(dimensions) + 1} "
                    f"({len(dimensions)} elements + 1 value)"
                )
            chunk[tuple(cell.strip() for cell in row[:-1])] = _coerce_data_value(row[-1])
            if len(chunk) >= DATA_CHUNK_ROWS:
                yield chunk, dimensions
                chunk = {}
        if chunk:
            yield chunk, dimensions


def _load_data(
    tm1: TM1Service,
    archive: zipfile.ZipFile,
    entries: list[_Entry],
    workers: int,
    failures: list[_Failure],
) -> None:
    """Stream every cube's data CSV back in parallel chunk writes.

    Chunks of one cube are written in parallel through a bounded window of
    outstanding futures; a malformed file fails only its own cube.
    """
    window = max(2, workers * 2)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for entry in entries:
            pending: list[Future] = []
            try:
                for chunk, dimensions in _iter_data_chunks(archive, entry):
                    pending.append(executor.submit(tm1.cubes.cells.write_values, entry.name, chunk, dimensions))
                    if len(pending) >= window:
                        _await_chunk(pending.pop(0), entry, failures)
            except (ValueError, csv.Error, UnicodeDecodeError) as exc:
                failures.append(_Failure(zipio.TYPE_DATA, entry.name, _error_text(exc)))
                for future in pending:
                    future.cancel()
                continue
            while pending:
                _await_chunk(pending.pop(0), entry, failures)


def _await_chunk(future: Future, entry: _Entry, failures: list[_Failure]) -> None:
    """Collect one chunk write, recording its failure if it raised."""
    try:
        future.result()
    except Exception as exc:
        failures.append(_Failure(zipio.TYPE_DATA, entry.name, _error_text(exc)))


def _load_security(
    tm1: TM1Service,
    archive: zipfile.ZipFile,
    entries: list[_Entry],
    failures: list[_Failure],
) -> list[str]:
    """Load security in order: groups, users, memberships, permissions."""
    notices: list[str] = []
    files = {entry.name: entry for entry in entries}

    for group in _read_group_names(archive, files):
        try:
            if not tm1.security.group_exists(group):
                tm1.security.create_group(group)
        except Exception as exc:
            failures.append(_Failure(zipio.TYPE_SECURITY, f"group {group!r}", _error_text(exc)))

    new_users: list[str] = []
    for body in _read_security_json(archive, files, "users"):
        name = body.get("Name")
        try:
            if not name:
                raise ValueError("user entry without a Name")
            if tm1.security.user_exists(name):
                _update_user_profile(tm1, name, body)
            else:
                # TM1 never exposes passwords, so a new user starts disabled
                # with a random one; an admin must set a real password.
                new_users.append(name)
                new_user = User(
                    name=name,
                    groups=[],
                    friendly_name=body.get("FriendlyName") or name,
                    password=secrets.token_urlsafe(RANDOM_PASSWORD_CHARS),
                    enabled=False,
                )
                tm1.security.create_user(new_user)
        except Exception as exc:
            failures.append(_Failure(zipio.TYPE_SECURITY, f"user {name!r}", _error_text(exc)))
    if new_users:
        notices.append(
            "created new users "
            + ", ".join(sorted(new_users))
            + " disabled with a random password — TM1 does not expose passwords, "
            "so an admin must set real passwords before they can log in"
        )

    for record in _read_security_json(archive, files, "client_groups"):
        client = record.get("client") or record.get("user")
        groups = record.get("groups") or []
        try:
            if not client:
                raise ValueError("membership record without a client")
            if groups:
                tm1.security.add_user_to_groups(client, groups)  # additive on purpose
        except Exception as exc:
            failures.append(_Failure(zipio.TYPE_SECURITY, f"memberships of {client!r}", _error_text(exc)))
    notices.append("group memberships are applied additively (never removed) so loading cannot lock anyone out")

    for record in _read_security_json(archive, files, "permissions"):
        object_type = record.get("object_type")
        object_name = record.get("object")
        group = record.get("group")
        permission = str(record.get("permission", "")).strip().upper()
        control_cube = PERMISSION_CONTROL_CUBES.get(object_type)
        try:
            if control_cube is None:
                raise ValueError(
                    f"unsupported permission object_type {object_type!r} "
                    f"(expected one of {sorted(PERMISSION_CONTROL_CUBES)})"
                )
            if permission not in VALID_PERMISSIONS:
                raise ValueError(f"invalid permission {record.get('permission')!r} (expected one of {VALID_PERMISSIONS})")
            tm1.cubes.cells.write_value(permission, control_cube, (group, object_name))
        except Exception as exc:
            failures.append(_Failure(zipio.TYPE_SECURITY, f"permission {group!r} on {object_name!r}", _error_text(exc)))
    return notices


def _update_user_profile(tm1: TM1Service, name: str, body: dict) -> None:
    """Update an existing user's profile only.

    Password, groups and enabled state are read from the server and written
    back untouched, so a reload can never lock an existing user out.
    """
    user = tm1.security.get_user(name)  # carries the user's current groups
    user.friendly_name = body.get("FriendlyName") or user.friendly_name
    if body.get("Type") is not None:
        user.user_type = body["Type"]
    tm1.security.update_user(user)


def _read_security_json(archive: zipfile.ZipFile, files: dict[str, _Entry], file_name: str) -> list:
    """Read one security/<name>.json; an absent file means nothing to load."""
    entry = files.get(file_name)
    if entry is None:
        return []
    return json.loads(archive.read(entry.file).decode("utf-8"))


def _read_group_names(archive: zipfile.ZipFile, files: dict[str, _Entry]) -> list[str]:
    """Read groups.json, accepting plain names or REST entities."""
    names: list[str] = []
    for item in _read_security_json(archive, files, "groups"):
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("Name"):
            names.append(item["Name"])
        else:
            raise ValueError(f"cannot read a group name from {item!r}")
    return names


def _print_plan(
    tm1: TM1Service,
    archive: zipfile.ZipFile,
    zip_path: str,
    entries_by_type: dict[str, list[_Entry]],
) -> None:
    """Print the planned create/overwrite/skip actions; touch nothing."""
    print(f"dry-run plan for {zip_path}:")
    for object_type in LOAD_ORDER:
        entries = entries_by_type.get(object_type, [])
        if not entries:
            continue
        if object_type == zipio.TYPE_DATA:
            cubes = ", ".join(sorted(entry.name for entry in entries))
            print(f"  data: {len(entries)} cube data file(s) to stream [{cubes}]")
        elif object_type == zipio.TYPE_SECURITY:
            _print_security_plan(tm1, archive, entries)
        else:
            creates: list[str] = []
            overwrites: list[str] = []
            for entry in entries:
                if _object_exists(tm1, entry):
                    overwrites.append(entry.name)
                else:
                    creates.append(entry.name)
            print(f"  {object_type}: {len(entries)} ({len(creates)} create, {len(overwrites)} overwrite)")
            for name in creates:
                print(f"    + create {name}")
            for name in overwrites:
                print(f"    ~ overwrite {name}")
    print("dry-run complete — nothing was written to the server")


def _print_security_plan(tm1: TM1Service, archive: zipfile.ZipFile, entries: list[_Entry]) -> None:
    """Print the planned security actions (groups, users, memberships, rights)."""
    files = {entry.name: entry for entry in entries}
    try:
        groups = _read_group_names(archive, files)
    except ValueError:
        groups = []
    group_creates = [group for group in groups if not tm1.security.group_exists(group)]
    users = [body.get("Name") for body in _read_security_json(archive, files, "users") if body.get("Name")]
    user_creates = [name for name in users if not tm1.security.user_exists(name)]
    memberships = _read_security_json(archive, files, "client_groups")
    permissions = _read_security_json(archive, files, "permissions")
    print(
        f"  security: {len(groups)} groups ({len(group_creates)} create, {len(groups) - len(group_creates)} skip), "
        f"{len(users)} users ({len(user_creates)} create disabled with random password, "
        f"{len(users) - len(user_creates)} profile-only overwrite), "
        f"{len(memberships)} membership record(s) to merge additively, "
        f"{len(permissions)} permission(s) to write"
    )


def _report_failures(failures: list[_Failure]) -> None:
    """Print the end-of-run failure summary; silent on a clean run."""
    if not failures:
        print("load complete: no failures")
        return
    print(f"load finished with {len(failures)} failure(s):", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure.object_type} {failure.name}: {failure.error}", file=sys.stderr)


def _error_text(exc: BaseException) -> str:
    """Best human-readable text for an exception."""
    return str(exc) or exc.__class__.__name__

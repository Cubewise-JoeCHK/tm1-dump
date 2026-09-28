"""Builders for the contract-valid fixture dump zip used by the load tests.

The fixture is generated deterministically (fixed timestamps, fixed zip
entry dates) so the committed ``tests/fixtures/basic_dump.zip`` can be
byte-compared against :func:`write_fixture_zip` output by a drift-guard
test. Hand-construct it after any contract change with::

    uv run python -c "from tests.fixture_dump import write_fixture_zip; \\
        write_fixture_zip('tests/fixtures/basic_dump.zip')"
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

from TM1py.Objects import (
    Chore,
    ChoreFrequency,
    ChoreStartTime,
    ChoreTask,
    Cube,
    Dimension,
    Element,
    ElementAttribute,
    Hierarchy,
    MDXView,
    NativeView,
    Process,
    Subset,
)

from tm1_dump import zipio
from tm1_dump.manifest import Manifest, ManifestObject, SourceInfo

FIXTURE_ZIP_PATH = Path(__file__).parent / "fixtures" / "basic_dump.zip"

FIXED_CREATED_AT = "2026-09-28T17:00:00+00:00"
FIXED_ZIP_DATE = (2026, 9, 28, 0, 0, 0)


def build_fixture_files() -> dict[str, bytes]:
    """Return every file of the fixture dump, including manifest.json."""
    files: dict[str, bytes] = {}
    manifest = Manifest(
        tool_version="0.1.0-test",
        created_at=FIXED_CREATED_AT,
        source=SourceInfo(server="fixture", address="fixture.internal", port=12354, tm1py_version="2.4.1"),
    )

    account_dimension = Dimension(name="Account")
    account_dimension.add_hierarchy(
        Hierarchy(
            name="Account",
            dimension_name="Account",
            elements=[
                Element("Revenue", "Numeric"),
                Element("Expenses", "Numeric"),
                Element("Margin", "Consolidated"),
            ],
            element_attributes=[ElementAttribute("Comment", "String")],
            edges={("Margin", "Revenue"): 1.0, ("Margin", "Expenses"): -1.0},
        )
    )
    files[zipio.build_path(zipio.TYPE_DIMENSIONS, "Account")] = _json_bytes(account_dimension.body)

    period_dimension = Dimension(name="Period")
    period_dimension.add_hierarchy(
        Hierarchy(
            name="Period",
            dimension_name="Period",
            elements=[Element("Jan", "Numeric"), Element("Feb", "Numeric"), Element("Mar", "Numeric")],
        )
    )
    files[zipio.build_path(zipio.TYPE_DIMENSIONS, "Period")] = _json_bytes(period_dimension.body)

    cube = Cube(name="P&L", dimensions=["Account", "Period"], rules="['Margin'] = ['Revenue'] - ['Expenses'];")
    files[zipio.build_path(zipio.TYPE_CUBES, "P&L")] = _json_bytes(cube.body)

    native_view = NativeView(cube_name="P&L", view_name="Default")
    files[zipio.build_path(zipio.TYPE_VIEWS, "Default", "P&L")] = _json_bytes(native_view.body)
    mdx_view = MDXView(cube_name="P&L", view_name="Top Revenue", MDX="SELECT {[Account].[Revenue]} ON 0 FROM [P&L]")
    files[zipio.build_path(zipio.TYPE_VIEWS, "Top Revenue", "P&L")] = _json_bytes(mdx_view.body)

    static_subset = Subset(subset_name="Top Lines", dimension_name="Account", elements=["Revenue", "Expenses"])
    files[zipio.build_path(zipio.TYPE_SUBSETS, "Top Lines", ("Account", "Account"))] = _json_bytes(static_subset.body)
    dynamic_subset = Subset(
        subset_name="Numeric", dimension_name="Account", expression="{[Account].[Account].Members}"
    )
    files[zipio.build_path(zipio.TYPE_SUBSETS, "Numeric", ("Account", "Account"))] = _json_bytes(dynamic_subset.body)

    import_process = Process(name="Import actuals", prolog_procedure="#Section Prolog\n#Fixture")
    files[zipio.build_path(zipio.TYPE_PROCESSES, "Import actuals")] = _json_bytes(import_process.body)
    export_process = Process(name="Export actuals")
    files[zipio.build_path(zipio.TYPE_PROCESSES, "Export actuals")] = _json_bytes(export_process.body)

    chore = Chore(
        name="Nightly",
        start_time=ChoreStartTime(2026, 1, 1, 2, 0, 0),
        dst_sensitivity=False,
        active=True,
        execution_mode="SingleCommit",
        frequency=ChoreFrequency(1, 0, 0, 0),
        tasks=[
            ChoreTask(step=0, process_name="Import actuals", parameters=[{"Name": "Version", "Value": "Actual"}])
        ],
    )
    files[zipio.build_path(zipio.TYPE_CHORES, "Nightly")] = _json_bytes(chore.body)

    files[zipio.build_path(zipio.TYPE_DATA, "P&L")] = _fixture_cube_data()

    files[zipio.build_path(zipio.TYPE_SECURITY, "groups")] = _json_bytes(["Planning", "Finance"])
    files[zipio.build_path(zipio.TYPE_SECURITY, "users")] = _json_bytes(
        [{"Name": "alice", "FriendlyName": "Alice"}, {"Name": "bob", "FriendlyName": "Bob"}]
    )
    files[zipio.build_path(zipio.TYPE_SECURITY, "client_groups")] = _json_bytes(
        [{"client": "alice", "groups": ["Planning", "Finance"]}]
    )
    files[zipio.build_path(zipio.TYPE_SECURITY, "permissions")] = _json_bytes(
        [{"object_type": "cubes", "object": "P&L", "group": "Planning", "permission": "read"}]
    )

    for path, payload in files.items():
        resolved = zipio.resolve_path(path)
        manifest.objects.append(
            ManifestObject(type=resolved.object_type, name=resolved.name, file=path, sha256=hashlib.sha256(payload).hexdigest())
        )
        manifest.counts[resolved.object_type] = manifest.counts.get(resolved.object_type, 0) + 1
    manifest.objects.sort(key=lambda spec: spec.file)
    files[zipio.MANIFEST_NAME] = manifest.to_json().encode("utf-8")
    return files


def write_fixture_zip(path: str | Path) -> None:
    """Write the deterministic fixture zip to ``path``."""
    files = build_fixture_files()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path_in_zip, payload in sorted(files.items()):
            archive.writestr(zipfile.ZipInfo(path_in_zip, date_time=FIXED_ZIP_DATE), payload)


def _fixture_cube_data() -> bytes:
    """Build data/P&L.csv: two header lines, then element/value rows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)  # RFC-4180 quoting so commas inside fields survive
    writer.writerow(["# cube", "P&L"])
    writer.writerow(["# dimensions", "Account", "Period"])
    writer.writerow(["Revenue", "Jan", "100.5"])
    writer.writerow(["Expenses", "Jan", "80"])
    writer.writerow(["Margin", "Jan", "n/a"])
    writer.writerow(["Revenue", "Feb", "1,234.5"])
    return buffer.getvalue().encode("utf-8")


def _json_bytes(body: str | list | dict) -> bytes:
    """TM1py object bodies are JSON strings; other values get dumped."""
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body, ensure_ascii=False).encode("utf-8")

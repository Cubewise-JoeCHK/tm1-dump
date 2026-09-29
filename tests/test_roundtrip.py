"""End-to-end roundtrip: the real dump engine feeding the real load engine.

A non-trivial model is built on :class:`~conftest.ModelTM1` (conftest):
dimensions with multiple hierarchies and element attributes, cubes with and
without rules, public views (native + MDX) and subsets (static + dynamic),
processes with parameters, a two-task chore, security with users, groups,
memberships and per-object rights, and cube data including string values,
an element name containing a comma, and an empty cube.

``run_dump`` produces a real zip; ``run_load`` reloads it onto a fresh
``ModelTM1``. The tests then compare source and target model state and pin
the dump<->load contract closed:

- control-cube rights replay in the control cube's real dimension order
  (``}ChoreSecurity`` stores ``}Groups`` first, the other cubes do not);
- named subsets exist before views load (dependency order);
- TM1py ``.body`` shapes (``@odata.bind`` refs) load through the loader's
  normalization for cubes and subsets;
- new users are created disabled with a random password (passwords never
  migrate), memberships apply additively, data CSV quoting survives commas.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile

import pytest
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
    User,
)

from conftest import CONTROL_CUBE_DIMENSIONS, ModelTM1, install_model_tm1, load_args
from tm1_dump import dump as dump_module
from tm1_dump import load as load_module
from tm1_dump import zipio
from tm1_dump.manifest import Manifest

ENV_VARS = ("TM1_ADDRESS", "TM1_PORT", "TM1_USER", "TM1_PASSWORD", "TM1_SSL", "TM1_NAMESPACE")

EXPECTED_COUNTS = {
    "dimensions": 3,
    "cubes": 4,
    "views": 2,
    "subsets": 5,
    "processes": 2,
    "chores": 1,
    "data": 4,
    "security": 4,
}

PLANNING_RULES = "['Margin'] = ['Revenue'] - ['Expenses'];"


# --------------------------------------------------------------------------- model builders


def build_source_model() -> ModelTM1:
    """A non-trivial model exercising every zip section the engines share."""
    tm1 = ModelTM1(server_name="RoundtripSource")

    # dimensions: multi-hierarchy + element attributes + a comma element name
    tm1.dimensions.update_or_create(
        Dimension(
            name="Account",
            hierarchies=[
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
            ],
        )
    )
    tm1.dimensions.update_or_create(
        Dimension(
            name="Month",
            hierarchies=[
                Hierarchy(
                    name="Month",
                    dimension_name="Month",
                    elements=[Element("Jan", "Numeric"), Element("Feb", "Numeric"), Element("Mar", "Numeric")],
                    element_attributes=[ElementAttribute("Comment", "String")],
                ),
                Hierarchy(
                    name="Quarter",
                    dimension_name="Month",
                    elements=[Element("Q1", "Consolidated"), Element("Jan", "Numeric"), Element("Feb", "Numeric")],
                    edges={("Q1", "Jan"): 1.0, ("Q1", "Feb"): 1.0},
                ),
            ],
        )
    )
    tm1.dimensions.update_or_create(
        Dimension(
            name="Region",
            hierarchies=[
                Hierarchy(
                    name="Region",
                    dimension_name="Region",
                    elements=[
                        Element("North", "Numeric"),
                        Element("South", "Numeric"),
                        Element("Sales, Total", "Consolidated"),
                    ],
                    element_attributes=[ElementAttribute("Currency", "String")],
                    edges={("Sales, Total", "North"): 1.0, ("Sales, Total", "South"): 1.0},
                )
            ],
        )
    )

    # cubes with and without rules
    tm1.cubes.update_or_create(Cube(name="P&L", dimensions=["Account", "Month"]))
    tm1.cubes.update_or_create_rules("P&L", PLANNING_RULES)
    tm1.cubes.update_or_create(Cube(name="Sales by Region", dimensions=["Region", "Month"]))
    tm1.cubes.update_or_create(Cube(name="Comments", dimensions=["Month"]))
    tm1.cubes.update_or_create(Cube(name="Empty", dimensions=["Month"]))

    # public subsets (static + dynamic, including the second hierarchy)
    tm1.subsets.update_or_create(
        Subset(subset_name="Top Lines", dimension_name="Account", elements=["Revenue", "Expenses"])
    )
    tm1.subsets.update_or_create(
        Subset(subset_name="Numeric", dimension_name="Account", expression="{[Account].[Account].Members}")
    )
    tm1.subsets.update_or_create(
        Subset(subset_name="All Months", dimension_name="Month", elements=["Jan", "Feb", "Mar"])
    )
    tm1.subsets.update_or_create(
        Subset(subset_name="First Quarter", dimension_name="Month", expression="{[Month].[Month].[Jan]}")
    )
    tm1.subsets.update_or_create(Subset(subset_name="All Quarters", dimension_name="Month", hierarchy_name="Quarter", elements=["Q1"]))

    # public views: native (referencing the named subsets) + MDX
    simple = NativeView(cube_name="P&L", view_name="Simple")
    simple.add_row("Account", Subset(subset_name="Top Lines", dimension_name="Account"))
    simple.add_column("Month", Subset(subset_name="All Months", dimension_name="Month"))
    simple.add_title("Month", "Jan", Subset(subset_name="All Months", dimension_name="Month"))
    tm1.views.update_or_create(simple)
    tm1.views.update_or_create(
        MDXView(
            cube_name="P&L",
            view_name="Top Revenue",
            MDX="SELECT {[Account].[Account].[Revenue]} ON 0 FROM [P&L]",
        )
    )

    # private objects must stay behind
    tm1.views.update_or_create(NativeView(cube_name="P&L", view_name="Scratch"), private=True)
    tm1.subsets.update_or_create(
        Subset(subset_name="Private Picks", dimension_name="Month", elements=["Jan"]), private=True
    )

    # processes: one with parameters, one without
    tm1.processes.update_or_create(
        Process(
            name="Load Actuals",
            prolog_procedure="#Section Prolog\nnRecordCount = 1;",
            epilog_procedure="#Section Epilog",
            parameters=[{"Name": "pVersion", "Prompt": "Version?", "Value": "Actual"}],
        )
    )
    tm1.processes.update_or_create(Process(name="Export Actuals", epilog_procedure="#Section Epilog\nViewDestroy;"))

    # chore with two tasks
    tm1.chores.update_or_create(
        Chore(
            name="Nightly",
            start_time=ChoreStartTime(2026, 1, 1, 2, 0, 0),
            dst_sensitivity=False,
            active=True,
            execution_mode="SingleCommit",
            frequency=ChoreFrequency(1, 0, 0, 0),
            tasks=[
                ChoreTask(0, "Load Actuals", parameters=[{"Name": "pVersion", "Value": "Actual"}]),
                ChoreTask(1, "Export Actuals", parameters=[]),
            ],
        )
    )

    # security: groups, users, memberships
    for group in ("ADMIN", "Data", "Planning"):
        tm1.security.create_group(group)
    tm1.security.create_user(User(name="admin", groups=["ADMIN", "Data"], friendly_name="Administrator"))
    tm1.security.create_user(User(name="tester", groups=["Data"], friendly_name="Tester"))
    tm1.security.create_user(User(name="viewer", groups=["Planning"], friendly_name="Viewer"))

    # per-object rights, stored positionally in the control cubes exactly
    # like a real server: }ChoreSecurity carries }Groups FIRST.
    tm1.cubes.cells.write_values(
        "}CubeSecurity",
        {("P&L", "ADMIN"): "Admin", ("P&L", "Planning"): "Read", ("Sales by Region", "Data"): "Write"},
    )
    tm1.cubes.cells.write_values("}DimensionSecurity", {("Account", "Data"): "Read"})
    tm1.cubes.cells.write_values("}ProcessSecurity", {})  # empty rights cube
    tm1.cubes.cells.write_values("}ChoreSecurity", {("ADMIN", "Nightly"): "Admin"})

    # cube data: numbers, a float, strings, a comma element name, an empty cube
    tm1.cubes.cells.write_values(
        "P&L",
        {("Revenue", "Jan"): 100, ("Expenses", "Jan"): 80, ("Margin", "Jan"): 20, ("Revenue", "Feb"): 12.5},
        ["Account", "Month"],
    )
    tm1.cubes.cells.write_values(
        "Sales by Region",
        {("Sales, Total", "Jan"): 999, ("North", "Feb"): 7},
        ["Region", "Month"],
    )
    tm1.cubes.cells.write_values("Comments", {("Jan",): "n/a", ("Feb",): "Actuals loaded"}, ["Month"])

    return tm1


def build_no_data_model() -> ModelTM1:
    """A small model for the ``--no-data`` roundtrip (issue #9).

    One regular cube with data, the ``}ElementAttributes_Month`` control
    cube carrying the dimension's attribute values (as a real server
    maintains it: dimensions ``[Month, }ElementAttributes_Month]``), and
    security. ``--no-data`` must keep the control cube's values while
    dropping the regular cube's data.
    """
    tm1 = ModelTM1(server_name="NoDataSource")

    tm1.dimensions.update_or_create(
        Dimension(
            name="Month",
            hierarchies=[
                Hierarchy(
                    name="Month",
                    dimension_name="Month",
                    elements=[Element("Jan", "Numeric"), Element("Feb", "Numeric")],
                    element_attributes=[ElementAttribute("Comment", "String")],
                )
            ],
        )
    )
    tm1.dimensions.update_or_create(
        Dimension(
            name="}ElementAttributes_Month",
            hierarchies=[
                Hierarchy(
                    name="}ElementAttributes_Month",
                    dimension_name="}ElementAttributes_Month",
                    elements=[Element("Comment", "String")],
                )
            ],
        )
    )

    tm1.cubes.update_or_create(Cube(name="Sales", dimensions=["Month"]))
    tm1.cubes.update_or_create(
        Cube(name="}ElementAttributes_Month", dimensions=["Month", "}ElementAttributes_Month"])
    )

    tm1.security.create_group("ADMIN")
    tm1.security.create_user(User(name="admin", groups=["ADMIN"], friendly_name="Administrator"))
    tm1.cubes.cells.write_values("}CubeSecurity", {("Sales", "ADMIN"): "Admin"})

    tm1.cubes.cells.write_values("Sales", {("Jan",): 100, ("Feb",): 42}, ["Month"])
    tm1.cubes.cells.write_values(
        "}ElementAttributes_Month",
        {("Jan", "Comment"): "Season start", ("Feb", "Comment"): "Short month"},
        ["Month", "}ElementAttributes_Month"],
    )
    return tm1


def build_stale_target(target: ModelTM1) -> None:
    """Pre-populate a target with stale state that ``--clean`` must replace."""
    target.dimensions.update_or_create(
        Dimension(
            name="Month",
            hierarchies=[Hierarchy(name="Month", dimension_name="Month", elements=[Element("Dec", "Numeric")])],
        )
    )
    target.subsets.update_or_create(
        Subset(subset_name="Stale Subset", dimension_name="Month", elements=["Dec"])
    )
    target.cubes.update_or_create(Cube(name="P&L", dimensions=["Account", "Month"]))
    target.cubes.update_or_create_rules("P&L", "['Stale'] = 1;")
    target.cubes.cells.write_values("P&L", {("Stale", "Dec"): 1}, ["Account", "Month"])
    target.views.update_or_create(NativeView(cube_name="P&L", view_name="Stale View"))
    target.processes.update_or_create(Process(name="Load Actuals", prolog_procedure="#Stale"))
    target.chores.update_or_create(
        Chore(
            name="Nightly",
            start_time=ChoreStartTime(2025, 12, 31, 23, 0, 0),
            dst_sensitivity=True,
            active=False,
            execution_mode="SingleCommit",
            frequency=ChoreFrequency(0, 0, 0, 0),
            tasks=[],
        )
    )
    target.security.create_group("Data")  # security objects are never deleted


# --------------------------------------------------------------------------- helpers


@pytest.fixture
def roundtrip_env(monkeypatch):
    """Clean TM1_* env plus a model-backed source/target pair in both engines."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    source, target = install_model_tm1(monkeypatch, source=build_source_model())
    return source, target


def _dump_args(out: str, **overrides) -> argparse.Namespace:
    defaults = {
        "address": "source.internal",
        "port": 12354,
        "user": "admin",
        "password": "apple",
        "ssl": None,
        "namespace": None,
        "config_file": None,
        "include": None,
        "exclude": None,
        "workers": None,
        "out": out,
        "no_data": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _dump_and_load(tmp_path, load_extra=()) -> tuple[str, int, int]:
    """Run the real dump on the wired source, then the real load on the target."""
    zip_path = str(tmp_path / "roundtrip.zip")
    dump_code = dump_module.run_dump(_dump_args(zip_path))
    load_code = load_module.run_load(load_args(zip_path, *load_extra))
    return zip_path, dump_code, load_code


def assert_models_equivalent(source: ModelTM1, target: ModelTM1) -> None:
    """Compare every object class the engines exchange, source vs target."""
    # dimensions: names, hierarchies, elements, edges, element attributes
    assert set(target.dimensions.get_all_names()) == set(source.dimensions.get_all_names())
    for dimension_name in source.dimensions.get_all_names():
        source_dimension = source.dimensions.get(dimension_name)
        target_dimension = target.dimensions.get(dimension_name)
        assert [h.name for h in target_dimension.hierarchies] == [h.name for h in source_dimension.hierarchies]
        for source_hierarchy, target_hierarchy in zip(
            source_dimension.hierarchies, target_dimension.hierarchies, strict=True
        ):
            assert {name: e.element_type for name, e in target_hierarchy.elements.items()} == {
                name: e.element_type for name, e in source_hierarchy.elements.items()
            }
            assert dict(target_hierarchy.edges) == dict(source_hierarchy.edges)
            assert [(a.name, a.attribute_type) for a in target_hierarchy.element_attributes] == [
                (a.name, a.attribute_type) for a in source_hierarchy.element_attributes
            ]

    # cubes + rules text
    assert {c.name for c in target.cubes.get_all()} == {c.name for c in source.cubes.get_all()}
    for source_cube in source.cubes.get_all():
        target_cube = next(c for c in target.cubes.get_all() if c.name == source_cube.name)
        assert target_cube.dimensions == source_cube.dimensions
        assert target_cube.rules == source_cube.rules

    # public views: same set, same class, same body
    cube_names = {c.name for c in source.cubes.get_all()}
    for cube_name in cube_names:
        source_public = source.views.get_all(cube_name)[1]
        target_public = target.views.get_all(cube_name)[1]
        assert {v.name for v in target_public} == {v.name for v in source_public}
        for source_view in source_public:
            target_view = next(v for v in target_public if v.name == source_view.name)
            assert type(target_view) is type(source_view)
            assert json.loads(target_view.body) == json.loads(source_view.body)
        assert target.views.get_all(cube_name)[0] == []  # no private views migrate

    # subsets: same (dimension, hierarchy, name) set, same kind and content
    for dimension_name in source.dimensions.get_all_names():
        for hierarchy_name in source.hierarchies.get_all_names(dimension_name):
            source_names = set(source.subsets.get_all_names(dimension_name, hierarchy_name))
            target_names = set(target.subsets.get_all_names(dimension_name, hierarchy_name))
            assert target_names == source_names, (dimension_name, hierarchy_name)
            assert target.subsets.get_all_names(dimension_name, hierarchy_name, private=True) == []
            for subset_name in source_names:
                source_subset = source.subsets.get(subset_name, dimension_name, hierarchy_name)
                target_subset = target.subsets.get(subset_name, dimension_name, hierarchy_name)
                assert target_subset.expression == source_subset.expression
                assert list(target_subset.elements or []) == list(source_subset.elements or [])
                assert target_subset.alias == source_subset.alias

    # processes: same names, same procedure text and parameters
    assert {p.name for p in target.processes.get_all()} == {p.name for p in source.processes.get_all()}
    for source_process in source.processes.get_all():
        target_process = next(p for p in target.processes.get_all() if p.name == source_process.name)
        assert target_process.prolog_procedure == source_process.prolog_procedure
        assert target_process.epilog_procedure == source_process.epilog_procedure
        assert target_process.parameters == source_process.parameters

    # chores: schedule, mode and tasks with parameters
    assert {c.name for c in target.chores.get_all()} == {c.name for c in source.chores.get_all()}
    for source_chore in source.chores.get_all():
        target_chore = next(c for c in target.chores.get_all() if c.name == source_chore.name)
        assert str(target_chore.start_time) == str(source_chore.start_time)
        assert str(target_chore.frequency) == str(source_chore.frequency)
        assert target_chore.execution_mode == source_chore.execution_mode
        assert target_chore.dst_sensitivity == source_chore.dst_sensitivity
        assert target_chore.active == source_chore.active
        assert [(t.process_name, t.parameters) for t in target_chore.tasks] == [
            (t.process_name, t.parameters) for t in source_chore.tasks
        ]

    # cube data, value for value
    for cube_name in cube_names:
        assert target.read_cells(cube_name) == source.read_cells(cube_name), cube_name

    # security: groups, users (profile), memberships, permissions
    assert set(target.security.get_all_groups()) == set(source.security.get_all_groups())
    source_users = {u.name: u for u in source.security.get_all_users()}
    target_users = {u.name: u for u in target.security.get_all_users()}
    assert set(target_users) == set(source_users)
    for user_name, source_user in source_users.items():
        target_user = target_users[user_name]
        assert target_user.friendly_name == source_user.friendly_name
        assert sorted(target.security.get_groups(user_name)) == sorted(source.security.get_groups(user_name))
        assert json.loads(target_user.body)["Type"] == json.loads(source_user.body)["Type"]
    for control_cube in CONTROL_CUBE_DIMENSIONS:
        assert target.read_cells(control_cube) == source.read_cells(control_cube), control_cube


def assert_manifest_is_valid(zip_path: str) -> Manifest:
    """Every manifest entry exists, hashes and layout-resolves; counts add up."""
    with zipfile.ZipFile(zip_path) as archive:
        manifest = Manifest.from_json(archive.read(zipio.MANIFEST_NAME).decode("utf-8"))
        names = set(archive.namelist())
        for entry in manifest.objects:
            assert entry.file in names
            assert entry.sha256 == hashlib.sha256(archive.read(entry.file)).hexdigest()
            resolved = zipio.resolve_path(entry.file)
            assert resolved.object_type == entry.type
            assert resolved.name == entry.name
    assert manifest.schema_version == 1
    assert manifest.errors == []
    assert manifest.counts == EXPECTED_COUNTS
    assert sum(manifest.counts.values()) == len(manifest.objects)
    assert manifest.source.server == "RoundtripSource"
    return manifest


# --------------------------------------------------------------------------- tests


def test_roundtrip_preserves_the_model(roundtrip_env, tmp_path, capsys):
    """Dump the source model, load it onto an empty target: state matches."""
    source, target = roundtrip_env
    zip_path, dump_code, load_code = _dump_and_load(tmp_path)

    assert dump_code == 0
    assert load_code == 0
    assert "load complete: no failures" in capsys.readouterr().out
    assert_models_equivalent(source, target)
    assert_manifest_is_valid(zip_path)


def test_roundtrip_dump_zip_carries_no_private_objects(roundtrip_env, tmp_path):
    """Private views/subsets stay on the source (public-only contract)."""
    source, _target = roundtrip_env
    zip_path, _dump_code, _load_code = _dump_and_load(tmp_path)
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert zipio.build_path(zipio.TYPE_VIEWS, "Scratch", "P&L") not in names
    assert zipio.build_path(zipio.TYPE_SUBSETS, "Private Picks", ("Month", "Month")) not in names
    assert len(source.views.get_all("P&L")[0]) == 1  # still there on the source


def test_roundtrip_never_migrates_passwords(roundtrip_env, tmp_path, capsys):
    """users.json has no passwords; new users start disabled with a random one."""
    _source, target = roundtrip_env
    zip_path, _dump_code, _load_code = _dump_and_load(tmp_path)

    with zipfile.ZipFile(zip_path) as archive:
        users = json.loads(archive.read(zipio.build_path(zipio.TYPE_SECURITY, "users")).decode("utf-8"))
    assert users, "the roundtrip model ships users"
    assert all("Password" not in user for user in users)

    created = [call for call in target.calls if call[0] == "create_user"]
    assert {call[2] for call in created} == {"admin", "tester", "viewer"}
    assert all("enabled=False" in call[3] and "password_set=True" in call[3] for call in created)

    output = capsys.readouterr().out
    assert "disabled with a random password" in output
    assert "admin, tester, viewer" in output


def test_roundtrip_dry_run_touches_nothing(roundtrip_env, tmp_path, capsys):
    """--dry-run on a fresh target prints the plan and writes nothing."""
    _source, target = roundtrip_env
    zip_path = str(tmp_path / "roundtrip.zip")
    assert dump_module.run_dump(_dump_args(zip_path)) == 0

    assert load_module.run_load(load_args(zip_path, "--dry-run")) == 0

    assert target.mutating_calls == []
    assert target.read_cells("P&L") == {}
    output = capsys.readouterr().out
    assert "dry-run plan" in output
    assert "+ create P&L" in output
    assert "data: 4 cube data file(s) to stream" in output
    assert "5 permission(s) to write" in output
    assert "nothing was written to the server" in output


def test_roundtrip_clean_replaces_stale_target(roundtrip_env, tmp_path, capsys):
    """--clean drops matching stale objects children-first, then reloads."""
    source, target = roundtrip_env
    build_stale_target(target)
    _zip_path, dump_code, load_code = _dump_and_load(tmp_path, load_extra=("--clean",))

    assert dump_code == 0
    assert load_code == 0
    output = capsys.readouterr().out
    assert "clean: dropped 4 matching object(s)" in output
    assert "load complete: no failures" in output

    deletes = [call for call in target.calls if call[0] == "delete"]
    assert [call[1] for call in deletes] == ["chores", "processes", "cubes", "dimensions"]
    calls = target.calls
    first_load_upsert = next(
        index
        for index, call in enumerate(calls)
        if call[0] == "upsert" and index > max(calls.index(delete) for delete in deletes)
    )
    assert all(calls.index(delete) < first_load_upsert for delete in deletes)
    assert {call[1] for call in deletes}.isdisjoint({"security"})  # security is never deleted

    # stale side effects of the deleted parents are gone; state matches source
    assert "Stale View" not in {v.name for v in target.views.get_all("P&L")[1]}
    assert "Stale Subset" not in target.subsets.get_all_names("Month", "Month")
    assert target.read_cells("P&L") == source.read_cells("P&L")  # stale cells replaced
    assert_models_equivalent(source, target)


def test_roundtrip_reload_is_idempotent(roundtrip_env, tmp_path, capsys):
    """Loading the same zip twice keeps the model equivalent (overwrite path)."""
    source, target = roundtrip_env
    zip_path = str(tmp_path / "roundtrip.zip")
    assert dump_module.run_dump(_dump_args(zip_path)) == 0

    assert load_module.run_load(load_args(zip_path)) == 0
    assert load_module.run_load(load_args(zip_path)) == 0

    second_pass_kinds = {call[1] for call in target.calls if call[0] == "upsert" and call[3] == "overwrite"}
    assert {"dimensions", "cubes", "processes", "chores"} <= second_pass_kinds
    assert "load complete: no failures" in capsys.readouterr().out
    assert_models_equivalent(source, target)


def test_roundtrip_no_data_keeps_attribute_values(monkeypatch, tmp_path, capsys):
    """dump --no-data -> load onto a fresh target: attribute values return,
    regular cube data stays behind, security rides along untouched."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    _source, target = install_model_tm1(monkeypatch, source=build_no_data_model())

    zip_path = str(tmp_path / "no_data.zip")
    assert dump_module.run_dump(_dump_args(zip_path, no_data=True)) == 0

    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert zipio.build_path(zipio.TYPE_DATA, "Sales") not in names
    assert zipio.build_path(zipio.TYPE_DATA, "}ElementAttributes_Month") in names
    assert zipio.build_path(zipio.TYPE_SECURITY, "permissions") in names

    assert load_module.run_load(load_args(zip_path)) == 0
    assert "load complete: no failures" in capsys.readouterr().out
    assert target.read_cells("Sales") == {}  # no regular cube data crossed over
    assert target.read_cells("}ElementAttributes_Month") == {
        ("Jan", "Comment"): "Season start",
        ("Feb", "Comment"): "Short month",
    }
    # permissions ride in security/permissions.json, not the data phase
    assert target.read_cells("}CubeSecurity") == {("Sales", "ADMIN"): "Admin"}

"""Unit tests for the load-side selection math (issue #11).

:func:`tm1_dump.load.plan_selection` is pure zip-side work: it takes the
grouped manifest entries plus the open archive and returns the effective
``--include``/``--exclude`` selection with the auto-pull markers — no
server anywhere. These tests run it against the contract-valid fixture
zip (dimensions Account + Period; cube P&L over both; two views on P&L;
two subsets on Account; processes Import/Export actuals; chore Nightly
tasking Import actuals; data for P&L; the four security files).
"""

from __future__ import annotations

import hashlib
import json
import zipfile

from conftest import load_args
from tm1_dump import load as load_module
from tm1_dump import zipio
from tm1_dump.filters import parse_filters

FULL_COUNTS = {
    zipio.TYPE_DIMENSIONS: 2,
    zipio.TYPE_CUBES: 1,
    zipio.TYPE_VIEWS: 2,
    zipio.TYPE_SUBSETS: 2,
    zipio.TYPE_PROCESSES: 2,
    zipio.TYPE_CHORES: 1,
    zipio.TYPE_DATA: 1,
    zipio.TYPE_SECURITY: 4,
}


def plan(zip_path, include=None, exclude=None) -> load_module.Selection:
    """Run plan_selection over the fixture zip's grouped entries."""
    filters = parse_filters(include, exclude)
    with zipfile.ZipFile(zip_path) as archive:
        manifest = load_module.Manifest.from_json(archive.read(zipio.MANIFEST_NAME).decode("utf-8"))
        entries_by_type = load_module._group_entries(manifest)
        return load_module.plan_selection(include, exclude, entries_by_type, archive, filters)


def names(selection: load_module.Selection, object_type: str) -> list[str]:
    """Selected object names of one type, in load order."""
    return [entry.name for entry in selection.entries_by_type.get(object_type, [])]


def assert_pulled(selection: load_module.Selection, object_type: str, name: str, puller: str) -> None:
    """The named entry exists and carries exactly this auto-pull note."""
    entry = next(item for item in selection.entries_by_type[object_type] if item.name == name)
    assert selection.auto_notes[entry.file] == [puller]


# --- no filters: everything, unchanged ---------------------------------------


def test_no_filters_selects_everything(fixture_zip_path):
    """Without --include/--exclude the selection is the full zip (default unchanged)."""
    selection = plan(fixture_zip_path)
    assert {object_type: len(entries) for object_type, entries in selection.entries_by_type.items()} == FULL_COUNTS
    assert selection.auto_notes == {}
    assert selection.unmatched == []


# --- each auto-pull rule -------------------------------------------------------


def test_selected_cube_pulls_its_dimensions_and_data(fixture_zip_path):
    """cubes=P&L brings Account + Period and data/P&L; nothing else rides along."""
    selection = plan(fixture_zip_path, include=["cubes=P&L"])
    assert names(selection, zipio.TYPE_CUBES) == ["P&L"]
    assert names(selection, zipio.TYPE_DIMENSIONS) == ["Account", "Period"]
    assert names(selection, zipio.TYPE_DATA) == ["P&L"]
    assert_pulled(selection, zipio.TYPE_DIMENSIONS, "Account", "cubes/P&L")
    assert_pulled(selection, zipio.TYPE_DIMENSIONS, "Period", "cubes/P&L")
    assert_pulled(selection, zipio.TYPE_DATA, "P&L", "cubes/P&L")
    for object_type in (zipio.TYPE_VIEWS, zipio.TYPE_SUBSETS, zipio.TYPE_PROCESSES, zipio.TYPE_CHORES, zipio.TYPE_SECURITY):
        assert names(selection, object_type) == []


def test_selected_view_pulls_its_cube_and_that_cubes_dependencies(fixture_zip_path):
    """views=Top Revenue pulls cube P&L, its dimensions and its data."""
    selection = plan(fixture_zip_path, include=["views=Top Revenue"])
    assert names(selection, zipio.TYPE_VIEWS) == ["Top Revenue"]
    assert names(selection, zipio.TYPE_CUBES) == ["P&L"]
    assert names(selection, zipio.TYPE_DIMENSIONS) == ["Account", "Period"]
    assert names(selection, zipio.TYPE_DATA) == ["P&L"]
    assert_pulled(selection, zipio.TYPE_CUBES, "P&L", "views/Top Revenue")
    assert_pulled(selection, zipio.TYPE_DIMENSIONS, "Account", "cubes/P&L")
    assert_pulled(selection, zipio.TYPE_DATA, "P&L", "cubes/P&L")


def test_selected_subset_pulls_its_dimension_only(fixture_zip_path):
    """subsets=Top Lines pulls dimension Account — no cube, no data."""
    selection = plan(fixture_zip_path, include=["subsets=Top Lines"])
    assert names(selection, zipio.TYPE_SUBSETS) == ["Top Lines"]
    assert names(selection, zipio.TYPE_DIMENSIONS) == ["Account"]
    assert_pulled(selection, zipio.TYPE_DIMENSIONS, "Account", "subsets/Top Lines")
    assert names(selection, zipio.TYPE_CUBES) == []
    assert names(selection, zipio.TYPE_DATA) == []


def test_selected_chore_pulls_only_its_task_processes(fixture_zip_path):
    """chores=Nightly brings Import actuals but not Export actuals."""
    selection = plan(fixture_zip_path, include=["chores=Nightly"])
    assert names(selection, zipio.TYPE_CHORES) == ["Nightly"]
    assert names(selection, zipio.TYPE_PROCESSES) == ["Import actuals"]
    assert_pulled(selection, zipio.TYPE_PROCESSES, "Import actuals", "chores/Nightly")


def test_direct_selection_never_wears_an_auto_marker(fixture_zip_path):
    """Objects matching the include patterns are direct selections: they
    never wear an auto marker even though the cube pull reaches them too."""
    selection = plan(fixture_zip_path, include=["cubes=P&L", "dimensions=*"])
    assert names(selection, zipio.TYPE_DIMENSIONS) == ["Account", "Period"]
    assert all(entry.file not in selection.auto_notes for entry in selection.entries_by_type[zipio.TYPE_DIMENSIONS])
    # the data file matched no include pattern of its own: pure auto-pull
    assert_pulled(selection, zipio.TYPE_DATA, "P&L", "cubes/P&L")


def test_narrowed_include_type_limits_auto_pull(fixture_zip_path):
    """A type the user narrowed with --include keeps that narrowing: pulled
    dependencies must pass the same filters, so dimensions=Account keeps
    the pulled cube's Period out."""
    selection = plan(fixture_zip_path, include=["cubes=P&L", "dimensions=Account"])
    assert names(selection, zipio.TYPE_DIMENSIONS) == ["Account"]


# --- exclude wins; data narrows ------------------------------------------------


def test_exclude_wins_over_include(fixture_zip_path):
    """An excluded object stays out even when an include pattern names it."""
    selection = plan(fixture_zip_path, include=["*"], exclude=["dimensions=Period"])
    assert names(selection, zipio.TYPE_DIMENSIONS) == ["Account"]
    assert names(selection, zipio.TYPE_CUBES) == ["P&L"]  # cube stays, its excluded dimension does not


def test_data_follows_cube_and_data_filters_narrow_on_top(fixture_zip_path):
    """--exclude "data=*" keeps every selected structure but drops cube data."""
    selection = plan(fixture_zip_path, include=["cubes=P&L"], exclude=["data=*"])
    assert names(selection, zipio.TYPE_CUBES) == ["P&L"]
    assert names(selection, zipio.TYPE_DIMENSIONS) == ["Account", "Period"]
    assert names(selection, zipio.TYPE_DATA) == []


def test_security_never_auto_pulled_only_explicit(fixture_zip_path):
    """Security loads with filters active only when explicitly selected."""
    selection = plan(fixture_zip_path, include=["cubes=P&L"])
    assert names(selection, zipio.TYPE_SECURITY) == []
    selection = plan(fixture_zip_path, include=["security=permissions"])
    assert names(selection, zipio.TYPE_SECURITY) == ["permissions"]
    assert names(selection, zipio.TYPE_CUBES) == []


# --- unmatched patterns and empty selections -----------------------------------


def test_unmatched_include_and_exclude_patterns_reported(fixture_zip_path):
    """Patterns matching nothing are named; matched ones are not listed."""
    selection = plan(fixture_zip_path, include=["cubes=P&*", "cubes=Sales*"], exclude=["data=Sales*"])
    assert selection.unmatched == ["cubes=Sales*", "data=Sales*"]


def test_bare_unmatched_pattern_reported(fixture_zip_path):
    """A bare pattern is unmatched when it names no object of any type."""
    selection = plan(fixture_zip_path, include=["zzz*"])
    assert selection.unmatched == ["zzz*"]
    assert names(selection, zipio.TYPE_CUBES) == []


def test_all_empty_selection_when_nothing_matches(fixture_zip_path):
    """A filter that matches nothing selects zero objects of every type."""
    selection = plan(fixture_zip_path, include=["cubes=Nope*"])
    assert all(not entries for entries in selection.entries_by_type.values())
    assert selection.auto_notes == {}


def test_case_insensitive_matching(fixture_zip_path):
    """Patterns and names match case-insensitively, like the dump filters."""
    selection = plan(fixture_zip_path, include=["cubes=p&l"])
    assert names(selection, zipio.TYPE_CUBES) == ["P&L"]
    assert len(names(selection, zipio.TYPE_DIMENSIONS)) == 2


def _view_zip(tmp_path) -> str:
    """A small dump zip whose native view binds two named subsets.

    Cube Stuff over Color(Red, Blue) + Size(Big, Small); subsets Warm and
    Cool on Color, Big on Size; native view Main binding Warm (rows) and
    Big (columns) — Cool is a decoy no view references.
    """
    from TM1py.Objects import Cube, Dimension, Element, Hierarchy, NativeView, Subset

    from tm1_dump.manifest import Manifest, ManifestObject, SourceInfo

    files: dict[str, bytes] = {}
    for dimension_name, element_names in (("Color", ["Red", "Blue"]), ("Size", ["Big", "Small"])):
        dimension = Dimension(
            name=dimension_name,
            hierarchies=[
                Hierarchy(
                    name=dimension_name,
                    dimension_name=dimension_name,
                    elements=[Element(element, "Numeric") for element in element_names],
                )
            ],
        )
        files[zipio.build_path(zipio.TYPE_DIMENSIONS, dimension_name)] = dimension.body.encode("utf-8")

    cube = Cube(name="Stuff", dimensions=["Color", "Size"])
    files[zipio.build_path(zipio.TYPE_CUBES, "Stuff")] = cube.body.encode("utf-8")

    for subset_name, dimension_name in (("Warm", "Color"), ("Cool", "Color"), ("Big", "Size")):
        subset = Subset(subset_name=subset_name, dimension_name=dimension_name, elements=["Red"])
        files[zipio.build_path(zipio.TYPE_SUBSETS, subset_name, (dimension_name, dimension_name))] = (
            subset.body.encode("utf-8")
        )

    view = NativeView(cube_name="Stuff", view_name="Main")
    view.add_row("Color", Subset(subset_name="Warm", dimension_name="Color"))
    view.add_column("Size", Subset(subset_name="Big", dimension_name="Size"))
    view.add_title("Size", "Big", Subset(subset_name="Big", dimension_name="Size"))
    files[zipio.build_path(zipio.TYPE_VIEWS, "Main", "Stuff")] = view.body.encode("utf-8")

    manifest = Manifest(
        tool_version="0.1.0-test",
        source=SourceInfo(server="viewzip", address="viewzip.internal", port=12354, tm1py_version="2.4.1"),
    )
    for path, payload in files.items():
        resolved = zipio.resolve_path(path)
        manifest.objects.append(
            ManifestObject(type=resolved.object_type, name=resolved.name, file=path, sha256=hashlib.sha256(payload).hexdigest())
        )
    files[zipio.MANIFEST_NAME] = manifest.to_json().encode("utf-8")

    zip_path = tmp_path / "view_dump.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for path_in_zip, payload in files.items():
            archive.writestr(path_in_zip, payload)
    return str(zip_path)


def test_view_subset_references_reads_both_body_shapes():
    """Subset refs are collected from @odata.bind and expanded JSON alike."""
    bind_view = json.dumps(
        {
            "Name": "Main",
            "Rows": [{"Subset@odata.bind": "Dimensions('Color')/Hierarchies('Color')/Subsets('Warm')"}],
            "Columns": [{"Subset@odata.bind": "Dimensions('Size')/Hierarchies('Size')/Subsets('Big')"}],
            "Titles": [],
        }
    )
    expanded_view = json.dumps(
        {
            "Name": "Main",
            "Rows": [{"Subset": {"Name": "Warm", "Hierarchy": {"Name": "Color", "Dimension": {"Name": "Color"}}}}],
            "Columns": [],
            "Titles": [],
        }
    )
    mdx_view = json.dumps({"Name": "Mdx", "MDX": "SELECT {[Color].[Color].[Red]} ON 0 FROM [Stuff]"})
    assert load_module._view_subset_references(bind_view) == [("Color", "Color", "Warm"), ("Size", "Size", "Big")]
    assert load_module._view_subset_references(expanded_view) == [("Color", "Color", "Warm")]
    assert load_module._view_subset_references(mdx_view) == []


def test_selected_view_pulls_the_named_subsets_it_references(tmp_path):
    """views=Main brings its cube, its dimensions and exactly the bound
    subsets — the unreferenced decoy subset stays behind."""
    zip_path = _view_zip(tmp_path)
    selection = plan(zip_path, include=["views=Main"])
    assert names(selection, zipio.TYPE_VIEWS) == ["Main"]
    assert names(selection, zipio.TYPE_CUBES) == ["Stuff"]
    assert names(selection, zipio.TYPE_DIMENSIONS) == ["Color", "Size"]
    assert names(selection, zipio.TYPE_SUBSETS) == ["Warm", "Big"]
    assert_pulled(selection, zipio.TYPE_SUBSETS, "Warm", "views/Main")
    assert_pulled(selection, zipio.TYPE_SUBSETS, "Big", "views/Main")
    assert "Cool" not in names(selection, zipio.TYPE_SUBSETS)


def test_excluded_subset_wins_over_view_pull(tmp_path):
    """--exclude subsets=Warm keeps it out; the view stays selected and its
    load fails per-object on a fresh target (documented error isolation)."""
    zip_path = _view_zip(tmp_path)
    selection = plan(zip_path, include=["views=Main"], exclude=["subsets=Warm"])
    assert names(selection, zipio.TYPE_VIEWS) == ["Main"]
    assert names(selection, zipio.TYPE_SUBSETS) == ["Big"]


def test_narrowed_subsets_include_limits_view_pull(tmp_path):
    """A --include subsets=... pattern keeps its narrowing: Warm is a direct
    selection and the other bound subset is suppressed, like the dims rule."""
    zip_path = _view_zip(tmp_path)
    selection = plan(zip_path, include=["views=Main", "subsets=Warm"])
    assert names(selection, zipio.TYPE_SUBSETS) == ["Warm"]
    warm = next(item for item in selection.entries_by_type[zipio.TYPE_SUBSETS] if item.name == "Warm")
    assert warm.file not in selection.auto_notes


def test_view_referencing_a_subset_twice_pulls_it_once(tmp_path):
    """A subset bound on two axes (column + title) carries the puller once."""
    zip_path = _view_zip(tmp_path)
    selection = plan(zip_path, include=["views=Main"])
    big = next(item for item in selection.entries_by_type[zipio.TYPE_SUBSETS] if item.name == "Big")
    assert selection.auto_notes[big.file] == ["views/Main"]


# --- end-to-end through run_load -------------------------------------------------


def test_nothing_selected_exits_1_before_connecting(fixture_zip_path, install_fake_tm1, capsys):
    """A selection of zero objects exits 1 with a clear message, no server contact."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(fixture_zip_path, "--include", "cubes=Nope*")) == 1
    stderr = capsys.readouterr().err
    assert "nothing to load" in stderr
    assert "cubes=Nope*" in stderr
    assert fake.connection_kwargs is None


def test_bad_filter_type_rejected(fixture_zip_path, install_fake_tm1, capsys):
    """An unknown TYPE=PATTERN fails with the shared parser's error."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(fixture_zip_path, "--include", "thing=*")) == 1
    assert "unknown object type" in capsys.readouterr().err
    assert fake.connection_kwargs is None

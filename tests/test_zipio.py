"""Tests for the zip layout contract (build/resolve symmetry)."""

import pytest

from tm1_dump import zipio


def test_layout_matches_contract():
    """The built paths must match the zip layout contract exactly."""
    assert zipio.build_path(zipio.TYPE_DIMENSIONS, "Account") == "dimensions/Account.json"
    assert zipio.build_path(zipio.TYPE_CUBES, "P&L") == "cubes/P%26L.json"
    assert zipio.build_path(zipio.TYPE_VIEWS, "Default", "P&L") == "views/P%26L/Default.json"
    assert zipio.build_path(zipio.TYPE_SUBSETS, "All", ("Products", "Retail")) == "subsets/Products/Retail/All.json"
    assert zipio.build_path(zipio.TYPE_PROCESSES, "Import actuals") == "processes/Import%20actuals.json"
    assert zipio.build_path(zipio.TYPE_CHORES, "Nightly") == "chores/Nightly.json"
    assert zipio.build_path(zipio.TYPE_DATA, "P&L") == "data/P%26L.csv"
    assert zipio.build_path(zipio.TYPE_SECURITY, "users") == "security/users.json"


@pytest.mark.parametrize(
    ("object_type", "name", "parent"),
    [
        (zipio.TYPE_DIMENSIONS, "Account", None),
        (zipio.TYPE_CUBES, "P&L", None),
        (zipio.TYPE_VIEWS, "Default", "P&L"),
        (zipio.TYPE_SUBSETS, "All", ("Products", "Retail")),
        (zipio.TYPE_PROCESSES, "Import actuals", None),
        (zipio.TYPE_CHORES, "Nightly", None),
        (zipio.TYPE_DATA, "P&L", None),
        (zipio.TYPE_SECURITY, "client_groups", None),
    ],
)
def test_build_resolve_roundtrip(object_type, name, parent):
    """resolve_path inverts build_path for every object type."""
    path = zipio.build_path(object_type, name, parent)
    expected_parents = parent if isinstance(parent, tuple) else ((parent,) if parent is not None else ())
    assert zipio.resolve_path(path) == zipio.ResolvedPath(object_type=object_type, name=name, parents=expected_parents)


@pytest.mark.parametrize("raw_name", ["Q1/FY", "a b", "100%", "café", "P&L", "a.b", "-~_"])
def test_special_characters_roundtrip(raw_name):
    """Names with special characters survive quoting as one path segment."""
    path = zipio.build_path(zipio.TYPE_CUBES, raw_name)
    assert path.count("/") == 1  # the name never leaks extra segments
    assert zipio.resolve_path(path).name == raw_name


@pytest.mark.parametrize("object_type", zipio.OBJECT_TYPES)
def test_extension_per_type(object_type):
    """Only cube data files are CSV; every other type is JSON."""
    parent = (
        ("Cube",)
        if object_type == zipio.TYPE_VIEWS
        else ("Dim", "Leaves")
        if object_type == zipio.TYPE_SUBSETS
        else None
    )
    path = zipio.build_path(object_type, "x", parent)
    if object_type == zipio.TYPE_DATA:
        assert path.endswith(zipio.DATA_EXT)
        assert not path.endswith(zipio.JSON_EXT)
    else:
        assert path.endswith(zipio.JSON_EXT)
        assert not path.endswith(zipio.DATA_EXT)


def test_security_file_names_constant():
    assert zipio.SECURITY_FILE_NAMES == ("users", "groups", "client_groups", "permissions")


def test_unknown_type_rejected():
    with pytest.raises(ValueError, match="unknown object type"):
        zipio.build_path("bogus", "x")


def test_empty_name_rejected():
    with pytest.raises(ValueError, match="name must not be empty"):
        zipio.build_path(zipio.TYPE_DIMENSIONS, "")


@pytest.mark.parametrize(
    ("object_type", "parent"),
    [
        (zipio.TYPE_VIEWS, None),  # missing cube parent
        (zipio.TYPE_SUBSETS, "OnlyDimension"),  # needs dimension AND hierarchy
        (zipio.TYPE_DIMENSIONS, "unexpected"),  # takes no parent at all
        (zipio.TYPE_DATA, ("a", "b")),  # takes no parent at all
    ],
)
def test_wrong_parent_depth_rejected(object_type, parent):
    with pytest.raises(ValueError, match="parent segment"):
        zipio.build_path(object_type, "x", parent)


def test_empty_parent_segment_rejected():
    with pytest.raises(ValueError, match="parent segment must not be empty"):
        zipio.build_path(zipio.TYPE_VIEWS, "Default", "")


@pytest.mark.parametrize(
    "path",
    [
        zipio.MANIFEST_NAME,  # zip root file, not an object
        "bogus/x.json",  # unknown top-level directory
        "dimensions/Account.txt",  # wrong extension
        "views/OnlyCube.json",  # missing the view name segment
        "subsets/Dim/All.json",  # missing the hierarchy segment
        "dimensions//Account.json",  # empty segment
        "dimensions/.json",  # empty name
    ],
)
def test_resolve_rejects_non_object_paths(path):
    with pytest.raises(ValueError):
        zipio.resolve_path(path)

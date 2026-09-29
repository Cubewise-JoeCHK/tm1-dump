"""Tests for the dump engine against a fake TM1Service surface."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile

import pytest
import requests

from tm1_dump import dump as dump_module
from tm1_dump import zipio
from tm1_dump.config import ConnectionConfig
from tm1_dump.dump import object_allowed, parse_filters
from tm1_dump.manifest import Manifest

ENV_VARS = ("TM1_ADDRESS", "TM1_PORT", "TM1_USER", "TM1_PASSWORD", "TM1_SSL", "TM1_NAMESPACE")


# --------------------------------------------------------------------------- fake TM1


class FakeBody:
    """Stand-in for a TM1py object exposing .name and .body."""

    def __init__(self, name: str, body: str, hierarchies: list | None = None) -> None:
        self.name = name
        self.body = body
        self.hierarchies = hierarchies or []  # the dump reads element attributes off each hierarchy


class FakeDimensionService:
    def __init__(self, bodies: dict[str, str], fail_on: set[str]) -> None:
        self._bodies = bodies
        self._fail_on = fail_on

    def get_all_names(self) -> list[str]:
        return list(self._bodies)

    def get(self, name: str) -> FakeBody:
        if name in self._fail_on:
            raise RuntimeError(f"dimension {name!r} exploded")
        return FakeBody(name, self._bodies[name])


class FakeHierarchyService:
    def __init__(self, hierarchies: dict[str, list[str]]) -> None:
        self._hierarchies = hierarchies

    def get_all_names(self, dimension_name: str) -> list[str]:
        return list(self._hierarchies.get(dimension_name, [dimension_name]))


class FakeCube:
    def __init__(self, name: str, dimensions: list[str]) -> None:
        self.name = name
        self.dimensions = dimensions
        body: dict = {"Name": name}
        if name == "Sales":
            body["Rules"] = "['x'] = 1;"  # rule-less cubes have no Rules key, like tm1py
        self.body = json.dumps(body)


class FakeCubeService:
    def __init__(self, cubes: list[FakeCube], dimension_names: dict[str, list[str]]) -> None:
        self._cubes = cubes
        self._dimension_names = dimension_names

    def get_all(self) -> list[FakeCube]:
        return list(self._cubes)

    def get_dimension_names(self, cube_name: str, **_kwargs) -> list[str]:
        return list(self._dimension_names[cube_name])


class FakeViewService:
    def __init__(self, public: dict[str, list[FakeBody]], private: dict[str, list[FakeBody]]) -> None:
        self._public = public
        self._private = private

    def get_all(self, cube_name: str, **_kwargs) -> tuple[list[FakeBody], list[FakeBody]]:
        """Mirror tm1py's return order: (private_views, public_views)."""
        return list(self._private.get(cube_name, [])), list(self._public.get(cube_name, []))


class FakeSubsetService:
    def __init__(self, subsets: dict[tuple[str, str], list[str]]) -> None:
        self._subsets = subsets

    def get_all_names(self, dimension_name: str, hierarchy_name: str = None, **_kwargs) -> list[str]:
        return list(self._subsets.get((dimension_name, hierarchy_name or dimension_name), []))

    def get(self, subset_name: str, dimension_name: str, hierarchy_name: str = None, **_kwargs) -> FakeBody:
        body = {"Name": subset_name, "Dimension@odata.bind": f"Dimensions('{dimension_name}')"}
        return FakeBody(subset_name, json.dumps(body))


class FakeSecurityService:
    def __init__(self, users: list[FakeBody], groups: list[str], memberships: dict[str, list[str]]) -> None:
        self._users = users
        self._groups = groups
        self._memberships = memberships

    def get_all_users(self) -> list[FakeBody]:
        return list(self._users)

    def get_all_groups(self) -> list[str]:
        return list(self._groups)

    def get_groups(self, user_name: str) -> list[str]:
        return list(self._memberships.get(user_name, []))


class FakeCellService:
    """Serves preset CSV per cube; tracks execute_mdx_csv calls."""

    def __init__(self, data: dict[str, list[str]]) -> None:
        self._data = data
        self.calls: list[dict] = []
        self.fail_with_blob = False

    def execute_mdx_csv(self, mdx: str, skip_zeros: bool = False, use_blob: bool = False, **_kwargs) -> str:
        self.calls.append({"mdx": mdx, "skip_zeros": skip_zeros, "use_blob": use_blob})
        if use_blob and self.fail_with_blob:
            raise RuntimeError("blob route unavailable")
        cube_name = mdx.rsplit("FROM [", 1)[1].removesuffix("]").replace("]]", "]")
        rows = self._data.get(cube_name, [])
        if not rows:
            return ""
        select_part = mdx.split("SELECT NON EMPTY ", 1)[1].split(" ON 0", 1)[0]
        dimensions = [
            member.removeprefix("{[").removesuffix("].MEMBERS}").replace("]]", "]")
            for member in select_part.split(" * ")
        ]
        header = ",".join(dimensions + ["Value"])
        return "\r\n".join([header, *rows])


class FakeServer:
    def get_server_name(self) -> str:
        return "TestServer"

    def get_product_version(self) -> str:
        return "11.8.00000.27"


class FakeTM1:
    """The exact service surface tm1_dump.dump touches."""

    def __init__(self) -> None:
        self.dimensions = FakeDimensionService(
            bodies={
                "Account": '{"Name":"Account"}',
                "Month": '{"Name":"Month"}',
                "Weird/Dim": '{"Name":"Weird/Dim"}',
                "}ElementAttributes_Month": '{"Name":"}ElementAttributes_Month"}',
            },
            fail_on=set(),
        )
        self.hierarchies = FakeHierarchyService({"Month": ["Month", "Quarter"]})
        cubes = [
            FakeCube("Sales", ["Month", "Account"]),
            FakeCube("Empty", ["Month"]),
            FakeCube("}ElementAttributes_Month", ["Month", "}ElementAttributes_Month"]),
        ]
        dimension_names = {
            "Sales": ["Month", "Account"],
            "Empty": ["Month"],
            "}ElementAttributes_Month": ["Month", "}ElementAttributes_Month"],
            "}CubeSecurity": ["}Cubes", "}Groups"],
            "}DimensionSecurity": ["}Dimensions", "}Groups"],
            "}ProcessSecurity": ["}Processes", "}Groups"],
            "}ChoreSecurity": ["}Groups", "}Chores"],
        }
        self.cubes = FakeCubeService(cubes, dimension_names)
        self.views = FakeViewService(
            public={"Sales": [FakeBody("Q1 View", '{"@odata.type":"#ibm.tm1.api.v1.NativeView"}')]},
            private={"Sales": [FakeBody("Private Scratch", "{}")]},
        )
        self.subsets = FakeSubsetService(
            {
                ("Month", "Month"): ["All Months", "Q1"],
                ("Month", "Quarter"): ["All Quarters"],
                ("Account", "Account"): ["Top Level"],
            }
        )
        self.processes = FakeProcessService()
        self.chores = FakeChoreService()
        self.security = FakeSecurityService(
            users=[
                FakeBody("admin", json.dumps({"Name": "admin", "Type": "Admin"})),
                FakeBody("tester", json.dumps({"Name": "tester", "Type": "User"})),
            ],
            groups=["Data", "ADMIN"],
            memberships={"admin": ["ADMIN", "Data"], "tester": ["Data"]},
        )
        self.cells = FakeCellService(
            data={
                "Sales": ["Jan,Revenue,100", "Feb,Revenue,200", "Jan,Expense,50"],
                "}ElementAttributes_Month": ["Jan,Comment,Season start", "Feb,Comment,Short month"],
                "}CubeSecurity": ["Sales,ADMIN,Admin", "Sales,Data,Write", "Plan,Data,Read"],
                "}ChoreSecurity": ["ADMIN,Nightly Load,Admin"],
            }
        )
        self.server = FakeServer()
        self.logout_calls = 0

    def logout(self) -> None:
        self.logout_calls += 1


class FakeProcessService:
    def get_all(self) -> list[FakeBody]:
        return [
            FakeBody("Load Actuals", '{"Name":"Load Actuals"}'),
            FakeBody("}control_proc", '{"Name":"}control_proc"}'),
        ]


class FakeChoreService:
    def get_all(self) -> list[FakeBody]:
        return [FakeBody("Nightly Load", '{"Name":"Nightly Load"}')]


# --------------------------------------------------------------------------- fixtures / helpers


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_tm1(monkeypatch):
    """Patch _connect to return a FakeTM1 and record the connection config."""
    tm1 = FakeTM1()
    captured: dict = {}

    def fake_connect(conn: ConnectionConfig) -> FakeTM1:
        captured["conn"] = conn
        return tm1

    monkeypatch.setattr(dump_module, "_connect", fake_connect)
    tm1.captured = captured  # type: ignore[attr-defined]
    return tm1


def _dump_args(out: str, **overrides) -> argparse.Namespace:
    defaults = {
        "address": "localhost",
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


def _read_zip(path: str) -> zipfile.ZipFile:
    """Open the dump zip for reading (caller may use it as a context manager)."""
    return zipfile.ZipFile(path)


# --------------------------------------------------------------------------- filter unit tests


def test_parse_filters_type_and_bare_patterns():
    filters = parse_filters(["dimensions=Ac*", "*prod*"], ["cubes=Sand*"])
    assert filters["include"]["dimensions"] == ["Ac*"]
    assert filters["include"]["*"] == ["*prod*"]
    assert filters["exclude"]["cubes"] == ["Sand*"]


def test_parse_filters_rejects_unknown_type():
    with pytest.raises(ValueError, match="unknown object type"):
        parse_filters(["thing=*"], None)


def test_parse_filters_ignores_empty_pattern():
    assert parse_filters(["dimensions="], []) == {
        "include": {**{object_type: [] for object_type in zipio.OBJECT_TYPES}, "*": []},
        "exclude": {**{object_type: [] for object_type in zipio.OBJECT_TYPES}, "*": []},
    }


def test_object_allowed_include_exclude_and_case():
    filters = parse_filters(["dimensions=Ac*"], ["*Attr*"])
    assert object_allowed("Account", filters, "dimensions") is True
    assert object_allowed("account", filters, "dimensions") is True  # case-insensitive name
    assert object_allowed("Month", filters, "dimensions") is False  # not matched by include
    assert object_allowed("}ElementAttributes_Account", filters, "dimensions") is False  # exclude wins


def test_object_allowed_bare_pattern_applies_to_every_type():
    filters = parse_filters(None, ["*test*"])
    assert object_allowed("Zebra test", filters, "processes") is False
    assert object_allowed("Sales", filters, "cubes") is True


def test_object_allowed_no_filters_means_everything():
    filters = parse_filters(None, None)
    assert object_allowed("Anything }At All", filters, "chores") is True


# --------------------------------------------------------------------------- connection


def test_service_kwargs_ssl_defaults_on():
    kwargs = dump_module._service_kwargs(ConnectionConfig(address="host", port=12354, user="admin", password="p"))
    assert kwargs == {"ssl": True, "address": "host", "port": 12354, "user": "admin", "password": "p"}


def test_service_kwargs_no_ssl_and_namespace_passthrough():
    conn = ConnectionConfig(address="host", port=12354, user="u", password="p", ssl=False, namespace="cam")
    assert dump_module._service_kwargs(conn) == {
        "ssl": False,
        "address": "host",
        "port": 12354,
        "user": "u",
        "password": "p",
        "namespace": "cam",
    }


def test_connect_receives_resolved_ssl_from_env(fake_tm1, tmp_path, monkeypatch):
    monkeypatch.setenv("TM1_SSL", "false")
    assert dump_module.run_dump(_dump_args(str(tmp_path / "d.zip"))) == 0
    assert fake_tm1.captured["conn"].ssl is False


def test_connection_summary_printed_before_connect(fake_tm1, tmp_path, capsys):
    """The resolved target, ssl setting and config source print to stderr pre-connect."""
    assert dump_module.run_dump(_dump_args(str(tmp_path / "d.zip"), password="s3cret-hunter2")) == 0
    err = capsys.readouterr().err
    assert err.startswith("target: localhost:12354 ssl=on user=admin (config: cli)")
    assert "s3cret-hunter2" not in err
    assert "==> dimensions:" in err  # the summary precedes the progress output


def test_ssl_connect_failure_hints_plain_http(tmp_path, monkeypatch, capsys):
    """A requests SSLError on connect gains the ssl=false hint, text kept visible."""

    def failing_connect(_conn):
        raise requests.exceptions.SSLError("handshake failed: WRONG_VERSION_NUMBER")

    monkeypatch.setattr(dump_module, "_connect", failing_connect)
    assert dump_module.run_dump(_dump_args(str(tmp_path / "d.zip"))) == 2
    err = capsys.readouterr().err
    assert "cannot connect to TM1" in err
    assert "WRONG_VERSION_NUMBER" in err
    assert "the server answered plain HTTP — set 'ssl = false' in config.ini or pass --no-ssl" in err


def test_non_ssl_connect_failure_message_unchanged(tmp_path, monkeypatch, capsys):
    def failing_connect(_conn):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(dump_module, "_connect", failing_connect)
    assert dump_module.run_dump(_dump_args(str(tmp_path / "d.zip"))) == 2
    err = capsys.readouterr().err
    assert "cannot connect to TM1: connection refused" in err
    assert "plain HTTP" not in err


# --------------------------------------------------------------------------- full dump


def test_dump_writes_full_zip_layout(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    assert dump_module.run_dump(_dump_args(out)) == 0
    expected_files = {
        zipio.MANIFEST_NAME,
        zipio.build_path(zipio.TYPE_DIMENSIONS, "Account"),
        zipio.build_path(zipio.TYPE_DIMENSIONS, "Month"),
        zipio.build_path(zipio.TYPE_DIMENSIONS, "Weird/Dim"),
        zipio.build_path(zipio.TYPE_DIMENSIONS, "}ElementAttributes_Month"),
        zipio.build_path(zipio.TYPE_CUBES, "Sales"),
        zipio.build_path(zipio.TYPE_CUBES, "Empty"),
        zipio.build_path(zipio.TYPE_CUBES, "}ElementAttributes_Month"),
        zipio.build_path(zipio.TYPE_VIEWS, "Q1 View", "Sales"),
        zipio.build_path(zipio.TYPE_SUBSETS, "All Months", ("Month", "Month")),
        zipio.build_path(zipio.TYPE_SUBSETS, "Q1", ("Month", "Month")),
        zipio.build_path(zipio.TYPE_SUBSETS, "All Quarters", ("Month", "Quarter")),
        zipio.build_path(zipio.TYPE_SUBSETS, "Top Level", ("Account", "Account")),
        zipio.build_path(zipio.TYPE_PROCESSES, "Load Actuals"),
        zipio.build_path(zipio.TYPE_PROCESSES, "}control_proc"),
        zipio.build_path(zipio.TYPE_CHORES, "Nightly Load"),
        zipio.build_path(zipio.TYPE_DATA, "Sales"),
        zipio.build_path(zipio.TYPE_DATA, "Empty"),
        zipio.build_path(zipio.TYPE_DATA, "}ElementAttributes_Month"),
        *(zipio.build_path(zipio.TYPE_SECURITY, stem) for stem in zipio.SECURITY_FILE_NAMES),
    }
    assert set(_read_zip(out).namelist()) == expected_files


def test_dump_special_characters_stay_single_segments(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    names = _read_zip(out).namelist()
    assert "dimensions/Weird%2FDim.json" in names  # slash percent-encoded, one segment
    manifest = _read_manifest(out)
    weird = next(entry for entry in manifest.objects if entry.type == "dimensions" and "Weird" in entry.file)
    assert weird.name == "Weird/Dim"  # manifest keeps the real name


def test_manifest_counts_and_sha256_match_zip(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    manifest = _read_manifest(out)
    expected_counts = {
        "dimensions": 4,
        "cubes": 3,
        "views": 1,
        "subsets": 4,
        "processes": 2,
        "chores": 1,
        "data": 3,
        "security": 4,
    }
    assert manifest.counts == expected_counts
    archive = _read_zip(out)
    zipped_names = set(archive.namelist())
    for entry in manifest.objects:
        assert entry.file in zipped_names
        digest = hashlib.sha256(archive.read(entry.file)).hexdigest()
        assert entry.sha256 == digest
    assert manifest.errors == []
    assert manifest.source.server == "TestServer"
    assert manifest.source.port == 12354
    assert manifest.filters == parse_filters(None, None)


def test_cube_json_carries_rules_text(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    with _read_zip(out) as archive:
        sales = json.loads(archive.read(zipio.build_path(zipio.TYPE_CUBES, "Sales")))
        empty = json.loads(archive.read(zipio.build_path(zipio.TYPE_CUBES, "Empty")))
    assert sales["Rules"] == "['x'] = 1;"
    assert "Rules" not in empty


def test_security_files_content(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    with _read_zip(out) as archive:
        users = json.loads(archive.read(zipio.build_path(zipio.TYPE_SECURITY, "users")))
        groups = json.loads(archive.read(zipio.build_path(zipio.TYPE_SECURITY, "groups")))
        client_groups = json.loads(archive.read(zipio.build_path(zipio.TYPE_SECURITY, "client_groups")))
        permissions = json.loads(archive.read(zipio.build_path(zipio.TYPE_SECURITY, "permissions")))
    assert [user["Name"] for user in users] == ["admin", "tester"]
    assert groups == ["ADMIN", "Data"]
    assert client_groups == {"admin": ["ADMIN", "Data"], "tester": ["Data"]}
    assert permissions["cubes"] == {"Plan": {"Data": "Read"}, "Sales": {"ADMIN": "Admin", "Data": "Write"}}
    # group column detected by header name, not position
    assert permissions["chores"] == {"Nightly Load": {"ADMIN": "Admin"}}
    assert set(permissions) == {"cubes", "dimensions", "processes", "chores"}


def test_private_views_are_not_dumped(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    assert "views/Private%20Scratch.json" not in " ".join(_read_zip(out).namelist())


def test_logout_called_after_dump(fake_tm1, tmp_path):
    dump_module.run_dump(_dump_args(str(tmp_path / "dump.zip")))
    assert fake_tm1.logout_calls == 1


# --------------------------------------------------------------------------- cube data CSV


def test_data_csv_header_and_sorted_rows(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    with _read_zip(out) as archive:
        content = archive.read(zipio.build_path(zipio.TYPE_DATA, "Sales")).decode("utf-8")
    assert content == (
        "# cube,Sales\r\n"
        "# dimensions,Month,Account\r\n"
        "Feb,Revenue,200\r\n"
        "Jan,Expense,50\r\n"
        "Jan,Revenue,100\r\n"
    )


def test_data_empty_cube_gets_header_only_csv(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    with _read_zip(out) as archive:
        content = archive.read(zipio.build_path(zipio.TYPE_DATA, "Empty")).decode("utf-8")
    assert content == "# cube,Empty\r\n# dimensions,Month\r\n"


def test_data_export_prefers_blob_route(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    assert all(call["use_blob"] for call in fake_tm1.cells.calls if "FROM [Sales]" in call["mdx"])
    assert all(call["skip_zeros"] for call in fake_tm1.cells.calls)


def test_data_export_falls_back_when_blob_fails(fake_tm1, tmp_path):
    fake_tm1.cells.fail_with_blob = True
    out = str(tmp_path / "dump.zip")
    assert dump_module.run_dump(_dump_args(out)) == 0  # fallback succeeds, no errors
    sales_calls = [call for call in fake_tm1.cells.calls if "FROM [Sales]" in call["mdx"]]
    empty_calls = [call for call in fake_tm1.cells.calls if "FROM [Empty]" in call["mdx"]]
    assert [call["use_blob"] for call in sales_calls] == [True, False]
    assert [call["use_blob"] for call in empty_calls] == [True, False]
    with _read_zip(out) as archive:
        content = archive.read(zipio.build_path(zipio.TYPE_DATA, "Sales")).decode("utf-8")
    assert "Jan,Revenue,100" in content


# --------------------------------------------------------------------------- filters on the dump


def test_include_filter_limits_types(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out, include=["dimensions=Ac*"]))
    names = _read_zip(out).namelist()
    assert zipio.build_path(zipio.TYPE_DIMENSIONS, "Account") in names
    assert zipio.build_path(zipio.TYPE_DIMENSIONS, "Month") not in " ".join(names)
    assert zipio.build_path(zipio.TYPE_CUBES, "Sales") in names  # other types unaffected
    manifest = _read_manifest(out)
    assert manifest.counts["dimensions"] == 1


def test_exclude_wins_over_include(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out, include=["dimensions=*"], exclude=["dimensions=*Attr*"]))
    names = _read_zip(out).namelist()
    assert zipio.build_path(zipio.TYPE_DIMENSIONS, "Month") in names
    assert zipio.build_path(zipio.TYPE_DIMENSIONS, "}ElementAttributes_Month") not in " ".join(names)


def test_bare_pattern_exclude_applies_across_types(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out, exclude=["}*"]))
    names = " ".join(_read_zip(out).namelist())
    manifest = _read_manifest(out)
    assert zipio.build_path(zipio.TYPE_DIMENSIONS, "}ElementAttributes_Month") not in names
    assert zipio.build_path(zipio.TYPE_PROCESSES, "}control_proc") not in names
    assert manifest.counts["dimensions"] == 3
    assert manifest.counts["processes"] == 1


def test_data_filter_selects_cubes_for_data(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out, include=["data=Sales"]))
    names = _read_zip(out).namelist()
    assert zipio.build_path(zipio.TYPE_DATA, "Sales") in names
    assert zipio.build_path(zipio.TYPE_DATA, "Empty") not in " ".join(names)
    assert zipio.build_path(zipio.TYPE_CUBES, "Empty") in names  # cube entities still dumped


def test_bad_filter_type_fails_fast(tmp_path):
    assert dump_module.run_dump(_dump_args(str(tmp_path / "d.zip"), include=["nope=*"])) == 2


# --------------------------------------------------------------------------- --no-data


def test_no_data_dumps_only_attribute_control_cube_data(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    assert dump_module.run_dump(_dump_args(out, no_data=True)) == 0
    names = _read_zip(out).namelist()
    assert zipio.build_path(zipio.TYPE_DATA, "Sales") not in " ".join(names)
    assert zipio.build_path(zipio.TYPE_DATA, "Empty") not in " ".join(names)
    assert zipio.build_path(zipio.TYPE_DATA, "}ElementAttributes_Month") in names
    # cube entities and everything else are unaffected — only the data phase narrows
    assert zipio.build_path(zipio.TYPE_CUBES, "Sales") in names
    assert zipio.build_path(zipio.TYPE_CUBES, "Empty") in names


def test_no_data_keeps_security_files_intact(fake_tm1, tmp_path):
    no_data_out = str(tmp_path / "no_data.zip")
    full_out = str(tmp_path / "full.zip")
    assert dump_module.run_dump(_dump_args(no_data_out, no_data=True)) == 0
    assert dump_module.run_dump(_dump_args(full_out)) == 0
    with _read_zip(no_data_out) as no_data_zip, _read_zip(full_out) as full_zip:
        for file_stem in zipio.SECURITY_FILE_NAMES:
            path = zipio.build_path(zipio.TYPE_SECURITY, file_stem)
            assert no_data_zip.read(path) == full_zip.read(path)


def test_no_data_manifest_counts_match_zip(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out, no_data=True))
    manifest = _read_manifest(out)
    data_files = [name for name in _read_zip(out).namelist() if name.startswith(f"{zipio.TYPE_DATA}/")]
    assert manifest.counts["data"] == len(data_files) == 1
    assert sum(manifest.counts.values()) == len(manifest.objects)


def test_no_data_gates_case_insensitively(fake_tm1, tmp_path):
    fake_tm1.cubes._cubes.append(FakeCube("}ELEMENTATTRIBUTES_Region", ["Region"]))
    fake_tm1.cells._data["}ELEMENTATTRIBUTES_Region"] = ["North,Currency,EUR"]
    out = str(tmp_path / "dump.zip")
    assert dump_module.run_dump(_dump_args(out, no_data=True)) == 0
    assert zipio.build_path(zipio.TYPE_DATA, "}ELEMENTATTRIBUTES_Region") in _read_zip(out).namelist()


def test_no_data_combines_with_include_and_exclude_filters(fake_tm1, tmp_path):
    narrowed_out = str(tmp_path / "narrowed.zip")
    dump_module.run_dump(_dump_args(narrowed_out, no_data=True, include=["data=*Attr*"]))
    narrowed_names = _read_zip(narrowed_out).namelist()
    assert zipio.build_path(zipio.TYPE_DATA, "}ElementAttributes_Month") in narrowed_names
    assert zipio.build_path(zipio.TYPE_DATA, "Sales") not in " ".join(narrowed_names)

    empty_out = str(tmp_path / "empty.zip")
    assert dump_module.run_dump(_dump_args(empty_out, no_data=True, exclude=["data=*"])) == 0
    assert not [name for name in _read_zip(empty_out).namelist() if name.startswith(f"{zipio.TYPE_DATA}/")]
    assert _read_manifest(empty_out).counts["data"] == 0


def test_default_run_dumps_all_cube_data(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    names = " ".join(_read_zip(out).namelist())
    for cube_name in ("Sales", "Empty", "}ElementAttributes_Month"):
        assert zipio.build_path(zipio.TYPE_DATA, cube_name) in names


# --------------------------------------------------------------------------- errors & parallelism


def test_one_failing_object_does_not_abort_batch(fake_tm1, tmp_path):
    fake_tm1.dimensions._fail_on.add("Month")
    out = str(tmp_path / "dump.zip")
    assert dump_module.run_dump(_dump_args(out)) == 1  # errors recorded, exit 1
    manifest = _read_manifest(out)
    assert manifest.errors == [
        {"type": "dimensions", "name": "Month", "error": "dimension 'Month' exploded"}
    ]
    assert manifest.counts["dimensions"] == 3  # the others survived
    names = " ".join(_read_zip(out).namelist())
    assert zipio.build_path(zipio.TYPE_DIMENSIONS, "Account") in names


def test_workers_fan_out_and_same_result(fake_tm1, tmp_path, monkeypatch):
    created: list[int] = []
    real_executor = dump_module.ThreadPoolExecutor

    def spy_executor(max_workers=None, **kwargs):
        created.append(max_workers)
        return real_executor(max_workers=max_workers, **kwargs)

    monkeypatch.setattr(dump_module, "ThreadPoolExecutor", spy_executor)

    serial_out = str(tmp_path / "serial.zip")
    parallel_out = str(tmp_path / "parallel.zip")
    dump_module.run_dump(_dump_args(serial_out, workers=1))
    dump_module.run_dump(_dump_args(parallel_out, workers=4))  # >=8 objects on the fake model

    # Pools are created per phase with min(workers, len(jobs)) workers; the
    # 4-job dimensions phase must fan out with all 4 workers, the serial run
    # must not build a pool at all.
    assert 4 in created
    assert all(workers <= 4 for workers in created)
    serial_files = _read_manifest(serial_out).objects
    parallel_files = _read_manifest(parallel_out).objects
    assert [(entry.type, entry.name, entry.file) for entry in serial_files] == [
        (entry.type, entry.name, entry.file) for entry in parallel_files
    ]


def test_workers_zero_or_negative_fall_back_to_default(fake_tm1, tmp_path):
    for bad_workers in (0, -3):
        out = str(tmp_path / f"dump_{bad_workers}.zip")
        assert dump_module.run_dump(_dump_args(out, workers=bad_workers)) == 0


# --------------------------------------------------------------------------- output file


def test_default_out_name_uses_server_and_timestamp(fake_tm1, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert dump_module.run_dump(_dump_args(out=None)) == 0
    dumps = list(tmp_path.glob("TestServer_*.zip"))
    assert len(dumps) == 1
    assert not list(tmp_path.glob("*.tmp"))  # atomic: no temp leftovers


def test_atomic_write_leaves_no_tmp_file(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    assert (tmp_path / "dump.zip").is_file()
    assert not (tmp_path / "dump.zip.tmp").exists()


def test_final_line_format(fake_tm1, tmp_path, capsys):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    final_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert final_line.startswith(f"wrote {out} (")
    assert final_line.endswith("s)")  # "... objects, ... MB, ...s)"


def test_progress_goes_to_stderr(fake_tm1, tmp_path, capsys):
    dump_module.run_dump(_dump_args(str(tmp_path / "dump.zip")))
    captured = capsys.readouterr()
    for object_type in zipio.OBJECT_TYPES:
        assert object_type in captured.err


def test_manifest_roundtrips_through_from_json(fake_tm1, tmp_path):
    out = str(tmp_path / "dump.zip")
    dump_module.run_dump(_dump_args(out))
    manifest = _read_manifest(out)
    assert Manifest.from_json(manifest.to_json()) == manifest


def _read_manifest(out: str) -> Manifest:
    with _read_zip(out) as archive:
        return Manifest.from_json(archive.read(zipio.MANIFEST_NAME).decode("utf-8"))

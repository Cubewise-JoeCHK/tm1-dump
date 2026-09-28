"""Tests for the load engine (issue #3) against a mocked TM1Service."""

import hashlib
import io
import json
import zipfile

import pytest

from conftest import load_args
from fixture_dump import FIXED_ZIP_DATE, FIXTURE_ZIP_PATH, build_fixture_files, write_fixture_zip
from tm1_dump import load as load_module
from tm1_dump import zipio

PHASE_KINDS = [
    zipio.TYPE_DIMENSIONS,
    zipio.TYPE_CUBES,
    zipio.TYPE_VIEWS,
    zipio.TYPE_SUBSETS,
    zipio.TYPE_PROCESSES,
    zipio.TYPE_CHORES,
    zipio.TYPE_DATA,
    zipio.TYPE_SECURITY,
]


def _phase_kinds_in_order(fake) -> list[str]:
    """The order in which object-type kinds first appear in the call log."""
    seen: list[str] = []
    for _, kind, _, _ in fake.calls:
        if kind and kind not in seen:
            seen.append(kind)
    return seen


def _calls_of(fake, action: str) -> list[tuple[str, str, str, str]]:
    return [call for call in fake.calls if call[0] == action]


def _rewrite_zip(src_path, dst_path, replacements: dict[str, bytes] | None = None, drop: set[str] | None = None) -> None:
    """Copy a zip, replacing or dropping members (fixed dates keep it deterministic)."""
    replacements = replacements or {}
    drop = drop or set()
    with zipfile.ZipFile(src_path) as source, zipfile.ZipFile(dst_path, "w") as target:
        for info in source.infolist():
            if info.filename in drop:
                continue
            payload = replacements.get(info.filename, source.read(info.filename))
            target.writestr(zipfile.ZipInfo(info.filename, date_time=(2026, 9, 28, 0, 0, 0)), payload)


def _manifest_text(zip_path) -> str:
    with zipfile.ZipFile(zip_path) as archive:
        return archive.read(zipio.MANIFEST_NAME).decode("utf-8")


# --- fixture integrity -------------------------------------------------------


def test_fixture_zip_is_contract_valid(tmp_path):
    """Every fixture file resolves through zipio and matches its manifest sha256."""
    zip_path = tmp_path / "fixture.zip"
    write_fixture_zip(zip_path)
    manifest = load_module.Manifest.from_json(_manifest_text(zip_path))
    assert manifest.schema_version == 1
    assert sum(manifest.counts.values()) == len(manifest.objects)
    problems = None
    with zipfile.ZipFile(zip_path) as archive:
        problems = load_module._verify_archive(archive, manifest)
    assert problems == []


def test_committed_fixture_matches_builder():
    """The committed tests/fixtures zip is byte-identical to the builder output."""
    assert FIXTURE_ZIP_PATH.exists(), "generate with fixture_dump.write_fixture_zip"
    committed = hashlib.sha256(FIXTURE_ZIP_PATH.read_bytes()).hexdigest()
    buffer = io.BytesIO()
    files = build_fixture_files()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path_in_zip, payload in sorted(files.items()):
            archive.writestr(zipfile.ZipInfo(path_in_zip, date_time=FIXED_ZIP_DATE), payload)
    assert committed == hashlib.sha256(buffer.getvalue()).hexdigest()


# --- happy path & dependency order -------------------------------------------


def test_load_happy_path_dependency_order(fixture_zip_path, install_fake_tm1, capsys):
    """A clean load hits every phase in dependency order and exits 0."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(fixture_zip_path)) == 0
    assert _phase_kinds_in_order(fake) == PHASE_KINDS
    assert "load complete: no failures" in capsys.readouterr().out


def test_load_creates_every_fixture_object(fixture_zip_path, install_fake_tm1):
    """Every model object is upserted via the create branch on an empty server."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(fixture_zip_path)) == 0
    upserts = _calls_of(fake, "upsert")
    assert len(upserts) == 10  # 2 dimensions, 1 cube, 2 views, 2 subsets, 2 processes, 1 chore
    for call in upserts:
        if call[1] in ("views", "subsets"):
            continue  # their detail carries the view class / subset kind instead
        assert call[3] == "create"


def test_load_overwrite_existing(fixture_zip_path, install_fake_tm1):
    """Existing objects go through the overwrite branch and still reload."""
    existing = {
        ("dimensions", "Account"),
        ("dimensions", "Period"),
        ("cubes", "P&L"),
        ("views", "Default"),
        ("views", "Top Revenue"),
        ("subsets", "Top Lines"),
        ("subsets", "Numeric"),
        ("processes", "Import actuals"),
        ("processes", "Export actuals"),
        ("chores", "Nightly"),
        ("group", "Planning"),
        ("group", "Finance"),
        ("user", "alice"),
    }
    fake = install_fake_tm1(existing=existing)
    assert load_module.run_load(load_args(fixture_zip_path)) == 0
    upserts = _calls_of(fake, "upsert")
    assert len(upserts) == 10
    for call in upserts:
        if call[1] in ("views", "subsets"):
            continue
        assert call[3] == "overwrite"
    # existing group/user are kept, not recreated
    assert _calls_of(fake, "create_group") == []
    assert [call[2] for call in _calls_of(fake, "create_user")] == ["bob"]


def test_views_and_subsets_dispatch_by_kind(fixture_zip_path, install_fake_tm1):
    """MDX vs native views and static vs dynamic subsets round-trip distinctly."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(fixture_zip_path)) == 0
    assert ("upsert", "views", "Default", "NativeView") in fake.calls
    assert ("upsert", "views", "Top Revenue", "MDXView") in fake.calls
    assert ("upsert", "subsets", "Top Lines", "static") in fake.calls
    assert ("upsert", "subsets", "Numeric", "dynamic") in fake.calls


def test_cube_rules_applied_after_cube(fixture_zip_path, install_fake_tm1):
    """The cube is created first, then its rules text is set."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(fixture_zip_path)) == 0
    actions = [call[0] for call in fake.calls]
    assert actions.index("upsert") >= 0  # sanity: calls recorded
    cube_upsert = next(call for call in fake.calls if call[0] == "upsert" and call[1] == "cubes")
    rules_upsert = next(call for call in fake.calls if call[0] == "upsert_rules")
    assert fake.calls.index(cube_upsert) < fake.calls.index(rules_upsert)
    assert "['Margin'] = ['Revenue'] - ['Expenses'];" in rules_upsert[3]


# --- --clean ------------------------------------------------------------------


def test_clean_drops_matching_objects_first(fixture_zip_path, install_fake_tm1, capsys):
    """--clean deletes children-before-parents and before any load."""
    existing = {("chores", "Nightly"), ("processes", "Import actuals"), ("cubes", "P&L"), ("dimensions", "Period")}
    fake = install_fake_tm1(existing=existing)
    assert load_module.run_load(load_args(fixture_zip_path, "--clean")) == 0
    deletes = _calls_of(fake, "delete")
    assert [call[1] for call in deletes] == ["chores", "processes", "cubes", "dimensions"]
    first_upsert = fake.calls.index(_calls_of(fake, "upsert")[0])
    assert all(fake.calls.index(delete) < first_upsert for delete in deletes)
    assert "clean: dropped 4 matching object(s)" in capsys.readouterr().out


def test_clean_ignores_absent_objects_and_never_deletes_security(fixture_zip_path, install_fake_tm1):
    """Only objects that exist are dropped; security is never deleted."""
    fake = install_fake_tm1()  # empty server: nothing to delete
    assert load_module.run_load(load_args(fixture_zip_path, "--clean")) == 0
    assert _calls_of(fake, "delete") == []


# --- --dry-run ----------------------------------------------------------------


def test_dry_run_touches_nothing(fixture_zip_path, install_fake_tm1, capsys):
    """--dry-run may read existence but must record zero mutating calls."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(fixture_zip_path, "--dry-run")) == 0
    assert fake.mutating_calls == []
    output = capsys.readouterr().out
    assert "dry-run plan" in output
    assert "nothing was written" in output


def test_dry_run_reports_create_and_overwrite(fixture_zip_path, install_fake_tm1, capsys):
    """The plan classifies each object as create or overwrite per type."""
    install_fake_tm1(existing={("dimensions", "Account"), ("cubes", "P&L")})
    assert load_module.run_load(load_args(fixture_zip_path, "--dry-run")) == 0
    output = capsys.readouterr().out
    assert "dimensions: 2 (1 create, 1 overwrite)" in output
    assert "~ overwrite Account" in output
    assert "+ create Period" in output
    assert "data: 1 cube data file(s) to stream [P&L]" in output


# --- integrity & validation ---------------------------------------------------


def test_tampered_zip_rejected(fixture_zip_path, tmp_path, install_fake_tm1, capsys):
    """A zip member that no longer matches its manifest sha256 stops the load."""
    with zipfile.ZipFile(fixture_zip_path) as archive:
        tampered = archive.read("dimensions/Period.json") + b" "
    tampered_path = tmp_path / "tampered.zip"
    _rewrite_zip(fixture_zip_path, tampered_path, replacements={"dimensions/Period.json": tampered})
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(str(tampered_path))) == 1
    stderr = capsys.readouterr().err
    assert "sha256 mismatch" in stderr
    assert "dimensions/Period.json" in stderr
    assert fake.connection_kwargs is None  # never even connected


def test_wrong_schema_version_rejected(fixture_zip_path, tmp_path, install_fake_tm1, capsys):
    """An unknown manifest schema_version is refused with a clear message."""
    manifest = json.loads(_manifest_text(fixture_zip_path))
    manifest["schema_version"] = manifest["schema_version"] + 1
    refused_path = tmp_path / "future.zip"
    _rewrite_zip(
        fixture_zip_path,
        refused_path,
        replacements={zipio.MANIFEST_NAME: json.dumps(manifest).encode("utf-8")},
    )
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(str(refused_path))) == 1
    assert "schema_version" in capsys.readouterr().err
    assert fake.connection_kwargs is None


def test_manifest_missing_file_rejected(fixture_zip_path, tmp_path, install_fake_tm1, capsys):
    """A file the manifest lists but the zip lacks stops the load."""
    missing_path = tmp_path / "incomplete.zip"
    _rewrite_zip(fixture_zip_path, missing_path, drop={"cubes/P%26L.json"})
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(str(missing_path))) == 1
    stderr = capsys.readouterr().err
    assert "missing from zip" in stderr
    assert fake.connection_kwargs is None


def test_missing_zip_file_reports_error(install_fake_tm1, capsys):
    """A nonexistent zip fails cleanly instead of raising."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args("/nonexistent/dump.zip")) == 1
    assert "cannot open zip" in capsys.readouterr().err
    assert fake.connection_kwargs is None


def test_missing_connection_settings_rejected(fixture_zip_path, install_fake_tm1, capsys, monkeypatch):
    """Without any address/port/user the load refuses before connecting."""
    monkeypatch.delenv("TM1_ADDRESS", raising=False)
    monkeypatch.delenv("TM1_PORT", raising=False)
    monkeypatch.delenv("TM1_USER", raising=False)
    fake = install_fake_tm1()
    from tm1_dump.cli import build_parser

    args = build_parser().parse_args(["load", fixture_zip_path])
    assert load_module.run_load(args) == 1
    stderr = capsys.readouterr().err
    assert "--address / TM1_ADDRESS" in stderr
    assert fake.connection_kwargs is None


# --- error isolation ----------------------------------------------------------


def test_one_bad_object_does_not_stop_the_rest(fixture_zip_path, install_fake_tm1, capsys):
    """A failing process is logged, other objects still load, exit code is 1."""
    fake = install_fake_tm1(fail={("processes", "Export actuals"): RuntimeError("boom")})
    assert load_module.run_load(load_args(fixture_zip_path)) == 1
    stderr = capsys.readouterr().err
    assert "processes Export actuals: boom" in stderr
    assert "load finished with 1 failure(s)" in stderr
    loaded_kinds = {call[1] for call in _calls_of(fake, "upsert")}
    assert loaded_kinds == {"dimensions", "cubes", "views", "subsets", "processes", "chores"}
    assert ("upsert", "processes", "Import actuals", "create") in fake.calls


def test_malformed_data_file_fails_only_its_cube(fixture_zip_path, tmp_path, install_fake_tm1, capsys):
    """A data CSV with a short row fails that cube but the model still loads."""
    bad_csv = b"# cube,P&L\n# dimensions,Account,Period\nRevenue,Jan\n"
    replacements = {zipio.build_path(zipio.TYPE_DATA, "P&L"): bad_csv}
    manifest = json.loads(_manifest_text(fixture_zip_path))
    for spec in manifest["objects"]:
        if spec["name"] == "P&L" and spec["type"] == zipio.TYPE_DATA:
            spec["sha256"] = hashlib.sha256(bad_csv).hexdigest()
    replacements[zipio.MANIFEST_NAME] = json.dumps(manifest).encode("utf-8")
    broken_path = tmp_path / "bad-data.zip"
    _rewrite_zip(fixture_zip_path, broken_path, replacements=replacements)
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(str(broken_path))) == 1
    stderr = capsys.readouterr().err
    assert "data P&L" in stderr
    assert ("upsert", "cubes", "P&L", "create") in fake.calls


# --- data streaming -----------------------------------------------------------


def test_data_streamed_as_coerced_cellsets(fixture_zip_path, install_fake_tm1):
    """Cube data is written through write_values with parsed dimensions and values."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(fixture_zip_path)) == 0
    assert len(fake.cell_writes) == 1
    cube_name, dimensions, cellset = fake.cell_writes[0]
    assert cube_name == "P&L"
    assert dimensions == ["Account", "Period"]
    assert cellset == {
        ("Revenue", "Jan"): 100.5,
        ("Expenses", "Jan"): 80,
        ("Margin", "Jan"): "n/a",
        ("Revenue", "Feb"): "1,234.5",  # quoted comma stays a string
    }


# --- security -----------------------------------------------------------------


def test_security_loads_groups_users_memberships_permissions_in_order(fixture_zip_path, install_fake_tm1, capsys):
    """Groups, then users, then memberships, then rights; new users disabled."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(fixture_zip_path)) == 0
    actions = [call[0] for call in fake.calls]

    def first(action: str) -> int:
        return actions.index(action)

    assert first("group_exists") < first("user_exists")
    assert first("user_exists") < first("add_user_to_groups")
    assert first("add_user_to_groups") < first("write_value")

    created = [call for call in _calls_of(fake, "create_user")]
    assert {call[2] for call in created} == {"alice", "bob"}
    assert all("enabled=False" in call[3] and "password_set=True" in call[3] for call in created)

    output = capsys.readouterr().out
    assert "random password" in output
    assert "alice, bob" in output
    assert ("add_user_to_groups", "security", "alice", "Planning,Finance") in fake.calls


def test_security_permissions_written_to_control_cubes(fixture_zip_path, install_fake_tm1):
    """Group rights land in the TM1 global-security control cubes, upper-cased."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(fixture_zip_path)) == 0
    assert (
        "write_value",
        "data",
        "}CubeSecurity",
        repr(("READ", ("Planning", "P&L"))),
    ) in fake.calls


def test_security_existing_user_gets_profile_only_update(fixture_zip_path, install_fake_tm1):
    """Existing users are updated without touching password, groups or state."""
    fake = install_fake_tm1(existing={("user", "alice"), ("group", "Planning")})
    assert load_module.run_load(load_args(fixture_zip_path)) == 0
    created = [call[2] for call in _calls_of(fake, "create_user")]
    assert created == ["bob"]
    updates = _calls_of(fake, "update_user")
    assert [call[2] for call in updates] == ["alice"]
    assert "friendly=Alice" in updates[0][3]
    # memberships still applied additively for the existing user
    assert ("add_user_to_groups", "security", "alice", "Planning,Finance") in fake.calls
    assert [call[2] for call in _calls_of(fake, "create_group")] == ["Finance"]


# --- connection ---------------------------------------------------------------


@pytest.mark.parametrize("extra,expected_ssl", [(["--no-ssl"], False), ([], True)])
def test_ssl_defaults_on_and_flag_turns_it_off(fixture_zip_path, install_fake_tm1, extra, expected_ssl):
    """SSL is on unless --no-ssl (or env/config) explicitly disables it."""
    fake = install_fake_tm1()
    assert load_module.run_load(load_args(fixture_zip_path, *extra)) == 0
    assert fake.connection_kwargs["ssl"] is expected_ssl

"""Tests for the CLI wiring and engine stubs."""

import pytest

from tm1_dump import dump as dump_module
from tm1_dump import load as load_module
from tm1_dump.cli import build_parser, main

DUMP_OPTIONS = (
    "--address",
    "--port",
    "--user",
    "--password",
    "--ssl",
    "--no-ssl",
    "--namespace",
    "--config-file",
    "--include",
    "--exclude",
    "--workers",
    "--out",
)

LOAD_OPTIONS = (
    "--address",
    "--port",
    "--user",
    "--password",
    "--ssl",
    "--no-ssl",
    "--namespace",
    "--config-file",
    "--workers",
    "--clean",
    "--dry-run",
)


def test_help_lists_both_subcommands(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "dump" in output
    assert "load" in output


def test_dump_help_lists_all_options(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["dump", "--help"])
    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    for option in DUMP_OPTIONS:
        assert option in output, f"missing {option} in dump --help"


def test_load_help_lists_all_options(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["load", "--help"])
    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    for option in LOAD_OPTIONS:
        assert option in output, f"missing {option} in load --help"


def test_subcommands_dispatch_to_engine_stubs():
    assert build_parser().parse_args(["dump"]).func is dump_module.run_dump
    assert build_parser().parse_args(["load", "dump.zip"]).func is load_module.run_load


def test_dump_stub_raises_not_implemented():
    """The dump engine is issue #2's work; load is implemented (issue #3)."""
    dump_args = build_parser().parse_args(["dump"])
    with pytest.raises(NotImplementedError):
        dump_module.run_dump(dump_args)


def test_subcommand_required():
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_load_takes_positional_zip():
    args = build_parser().parse_args(["load", "some dump.zip"])
    assert args.zip_file == "some dump.zip"
    assert args.clean is False
    assert args.dry_run is False

"""Tests for ``tm1-dump init`` and implicit ./config.ini discovery."""

import argparse

import pytest

from tm1_dump import initcmd
from tm1_dump.cli import main
from tm1_dump.config import ConnectionConfig, connection_problem, resolve_connection

ENV_VARS = ("TM1_ADDRESS", "TM1_PORT", "TM1_USER", "TM1_PASSWORD", "TM1_SSL", "TM1_NAMESPACE")


def _args(**overrides) -> argparse.Namespace:
    """Build a parsed-args namespace with all connection options unset by default."""
    defaults = {
        "address": None,
        "port": None,
        "user": None,
        "password": None,
        "ssl": None,
        "namespace": None,
        "config_file": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _clear_env(monkeypatch) -> None:
    """Remove every TM1_* environment variable so tests see only file/CLI layers."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# --- tm1-dump init ------------------------------------------------------------


def test_init_writes_parseable_template(tmp_path, monkeypatch, capsys):
    """A fresh config.ini parses cleanly and starts with nothing configured."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    config_path = tmp_path / "config.ini"
    assert config_path.is_file()
    resolved = resolve_connection(_args(config_file=str(config_path)))
    assert resolved == ConnectionConfig()
    out = capsys.readouterr().out
    assert "edit config.ini" in out
    assert "tm1-dump dump" in out


def test_init_refuses_to_clobber_without_force(tmp_path, monkeypatch, capsys):
    """The second init without --force exits non-zero and leaves the file alone."""
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    (tmp_path / "config.ini").write_text("# my hand-edited config", encoding="utf-8")
    assert main(["init"]) == 1
    assert (tmp_path / "config.ini").read_text(encoding="utf-8") == "# my hand-edited config"
    assert "--force" in capsys.readouterr().err


def test_init_force_overwrites(tmp_path, monkeypatch):
    """--force replaces an existing config.ini with the template."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text("# old", encoding="utf-8")
    assert main(["init", "--force"]) == 0
    assert (tmp_path / "config.ini").read_text(encoding="utf-8") == initcmd.TEMPLATE


# --- implicit ./config.ini discovery -------------------------------------------


def test_dump_args_discover_config_ini_in_cwd(tmp_path, monkeypatch):
    """With only a ./config.ini, resolve_connection picks the connection up from it."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text(
        "[tm1]\naddress = file.tm1\nport = 11111\nuser = admin\npassword = secret\n",
        encoding="utf-8",
    )
    resolved = resolve_connection(_args())
    assert resolved == ConnectionConfig(address="file.tm1", port=11111, user="admin", password="secret")


def test_missing_config_ini_behaves_like_no_file(tmp_path, monkeypatch):
    """Without ./config.ini nothing is configured — same as before discovery existed."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert resolve_connection(_args()) == ConnectionConfig()


def test_precedence_cli_over_env_over_implicit_config(tmp_path, monkeypatch):
    """CLI args still beat env vars, which still beat the implicit ./config.ini."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text("[tm1]\naddress = file.tm1\nport = 11111\n", encoding="utf-8")
    monkeypatch.setenv("TM1_ADDRESS", "env.tm1")
    monkeypatch.setenv("TM1_PORT", "22222")
    assert resolve_connection(_args(address="cli.tm1")).address == "cli.tm1"
    assert resolve_connection(_args(address="cli.tm1")).port == 22222
    assert resolve_connection(_args()).address == "env.tm1"
    assert resolve_connection(_args()).port == 22222


def test_implicit_config_ini_without_tm1_section_raises(tmp_path, monkeypatch):
    """A ./config.ini lacking [tm1] is reported, not silently ignored."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text("[other]\naddress = x\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[tm1\]"):
        resolve_connection(_args())


# --- no-connection remedy -------------------------------------------------------


def test_no_connection_error_mentions_init(tmp_path, monkeypatch):
    """With only a fresh template present, the remedy names tm1-dump init."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    problem = connection_problem(resolve_connection(_args()))
    assert problem is not None
    assert "tm1-dump init" in problem


def test_bare_dump_without_any_config_says_init(tmp_path, monkeypatch, capsys):
    """tm1-dump dump with nothing configured anywhere fails with the init remedy."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(["dump", "--out", str(tmp_path / "x.zip")]) == 2
    assert "tm1-dump init" in capsys.readouterr().err

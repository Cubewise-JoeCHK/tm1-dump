"""Tests for connection settings precedence (CLI > env vars > config file)."""

import argparse

import pytest

from tm1_dump.config import ConnectionConfig, resolve_connection

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


def _write_config(tmp_path, body: str) -> str:
    """Write an ini file into ``tmp_path`` and return its path."""
    config_file = tmp_path / "tm1_config.ini"
    config_file.write_text(body, encoding="utf-8")
    return str(config_file)


def test_cli_wins_over_env_and_file(tmp_path, monkeypatch):
    config_file = _write_config(tmp_path, "[tm1]\naddress = file.tm1\nport = 11111\n")
    monkeypatch.setenv("TM1_ADDRESS", "env.tm1")
    monkeypatch.setenv("TM1_PORT", "22222")
    monkeypatch.setenv("TM1_SSL", "false")
    resolved = resolve_connection(_args(address="cli.tm1", port=33333, config_file=config_file))
    assert resolved.address == "cli.tm1"
    assert resolved.port == 33333
    assert resolved.ssl is False


def test_env_wins_over_config_file(tmp_path, monkeypatch):
    config_file = _write_config(tmp_path, "[tm1]\naddress = file.tm1\nport = 11111\nuser = fileuser\n")
    monkeypatch.setenv("TM1_ADDRESS", "env.tm1")
    resolved = resolve_connection(_args(config_file=config_file))
    assert resolved.address == "env.tm1"
    assert resolved.user == "fileuser"


def test_config_file_used_when_nothing_higher(tmp_path, monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    config_file = _write_config(
        tmp_path,
        "[tm1]\naddress = file.tm1\nport = 11111\nuser = admin\npassword = secret\nssl = true\nnamespace = camns\n",
    )
    resolved = resolve_connection(_args(config_file=config_file))
    assert resolved == ConnectionConfig(
        address="file.tm1", port=11111, user="admin", password="secret", ssl=True, namespace="camns"
    )


def test_no_sources_yields_all_unset(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert resolve_connection(_args()) == ConnectionConfig()


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_connection(_args(config_file=str(tmp_path / "nope.ini")))


def test_config_file_without_tm1_section_raises(tmp_path):
    config_file = _write_config(tmp_path, "[other]\naddress = x\n")
    with pytest.raises(ValueError, match=r"\[tm1\]"):
        resolve_connection(_args(config_file=config_file))


def test_bad_env_port_raises(monkeypatch):
    monkeypatch.setenv("TM1_PORT", "not-a-number")
    with pytest.raises(ValueError, match="TM1_PORT"):
        resolve_connection(_args())


def test_bad_env_ssl_raises(monkeypatch):
    monkeypatch.setenv("TM1_SSL", "maybe")
    with pytest.raises(ValueError, match="TM1_SSL"):
        resolve_connection(_args())


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "0", "false", "no", "off"])
def test_ssl_boolean_parsing(monkeypatch, raw):
    monkeypatch.setenv("TM1_SSL", raw)
    resolved = resolve_connection(_args())
    assert resolved.ssl is (raw.strip().lower() in {"1", "true", "yes", "on"})

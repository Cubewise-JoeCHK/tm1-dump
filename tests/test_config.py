"""Tests for connection settings precedence (CLI > env vars > config file)."""

import argparse

import pytest
import requests

from tm1_dump.config import SSL_HINT, ConnectionConfig, connection_error_text, describe_connection, resolve_connection

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


# --------------------------------------------------------------------------- diagnostics (issue #14)


def test_describe_connection_shows_file_source_and_no_password(tmp_path):
    config_file = _write_config(
        tmp_path,
        "[tm1]\naddress = file.tm1\nport = 11111\nuser = admin\npassword = s3cret-hunter2\nssl = false\n",
    )
    args = _args(config_file=config_file)
    line = describe_connection(resolve_connection(args), args)
    assert line == f"target: file.tm1:11111 ssl=off user=admin (config: {config_file})"
    assert "s3cret-hunter2" not in line


def test_describe_connection_discovered_file_wins_label(tmp_path, monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text("[tm1]\naddress = disc.tm1\nport = 22222\nuser = ops\n", encoding="utf-8")
    args = _args(address="cli.tm1", port=33333, user="cliuser", password="s3cret-hunter2")
    line = describe_connection(resolve_connection(args), args)
    # the file path wins the label even though the CLI values win the precedence
    assert line == f"target: cli.tm1:33333 ssl=on user=cliuser (config: {tmp_path / 'config.ini'})"
    assert "s3cret-hunter2" not in line


def test_describe_connection_env_source(tmp_path, monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no config.ini here
    monkeypatch.setenv("TM1_ADDRESS", "env.tm1")
    line = describe_connection(resolve_connection(_args()), _args())
    assert line.endswith("(config: env)")


def test_describe_connection_cli_source(tmp_path, monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    args = _args(address="cli.tm1", port=33333, user="u", password="s3cret-hunter2")
    line = describe_connection(resolve_connection(args), args)
    assert line.endswith("(config: cli)")
    assert "s3cret-hunter2" not in line


def test_describe_connection_defaults_source(tmp_path, monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    args = _args()
    line = describe_connection(resolve_connection(args), args)
    assert line.endswith("(config: defaults)")


def test_connection_error_text_appends_hint_on_ssl_error():
    error = requests.exceptions.SSLError("handshake failed: WRONG_VERSION_NUMBER")
    text = connection_error_text(error)
    assert text.startswith("handshake failed: WRONG_VERSION_NUMBER")  # original text stays visible
    assert text.endswith(SSL_HINT)


def test_connection_error_text_wrapped_ssl_error():
    """An SSL error TM1py wrapped into another exception still gains the hint."""
    try:
        raise requests.exceptions.SSLError("WRONG_VERSION_NUMBER")
    except requests.exceptions.SSLError as inner:
        wrapped = RuntimeError("cannot connect to TM1")
        wrapped.__cause__ = inner  # assigned inside the block: Python deletes `inner` at block exit
    assert SSL_HINT in connection_error_text(wrapped)


def test_connection_error_text_wrapped_via_context():
    """The __context__ chain counts too (raise inside an except block, no from)."""
    try:
        raise requests.exceptions.SSLError("WRONG_VERSION_NUMBER")
    except requests.exceptions.SSLError:
        try:
            raise RuntimeError("connect failed")  # __context__ is set on raise
        except RuntimeError as caught:
            wrapped = caught
    assert SSL_HINT in connection_error_text(wrapped)


def test_connection_error_text_other_failures_unchanged():
    assert connection_error_text(RuntimeError("connection refused")) == "connection refused"
    assert connection_error_text(RuntimeError()) == "RuntimeError"  # empty message: class name
    hint_free = connection_error_text(ValueError("bad port"))
    assert "bad port" in hint_free
    assert SSL_HINT not in hint_free

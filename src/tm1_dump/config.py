"""Connection settings resolution shared by the dump and load engines.

Precedence: CLI arguments > environment variables (``TM1_ADDRESS``,
``TM1_PORT``, ``TM1_USER``, ``TM1_PASSWORD``, ``TM1_SSL``,
``TM1_NAMESPACE``) > config file (TM1py-style ini with a ``[tm1]``
section, passed via ``--config-file``).
"""

from __future__ import annotations

import argparse
import configparser
import os
from dataclasses import dataclass, fields

CONFIG_SECTION = "tm1"

ENV_ADDRESS = "TM1_ADDRESS"
ENV_PORT = "TM1_PORT"
ENV_USER = "TM1_USER"
ENV_PASSWORD = "TM1_PASSWORD"
ENV_SSL = "TM1_SSL"
ENV_NAMESPACE = "TM1_NAMESPACE"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


@dataclass
class ConnectionConfig:
    """Resolved TM1 connection settings (``None`` = not set anywhere)."""

    address: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    ssl: bool | None = None
    namespace: str | None = None


def resolve_connection(args: argparse.Namespace) -> ConnectionConfig:
    """Resolve connection settings: CLI args, then env vars, then config file.

    For each setting the first layer that provides a value wins.
    """
    layers = (_layer_from_cli(args), _layer_from_env(os.environ), _layer_from_file(args.config_file))
    resolved = ConnectionConfig()
    for setting in fields(ConnectionConfig):
        for layer in layers:
            value = getattr(layer, setting.name)
            if value is not None:
                setattr(resolved, setting.name, value)
                break
    return resolved


def _layer_from_cli(args: argparse.Namespace) -> ConnectionConfig:
    """Collect the connection options set on the command line."""
    return ConnectionConfig(
        address=args.address,
        port=args.port,
        user=args.user,
        password=args.password,
        ssl=args.ssl,
        namespace=args.namespace,
    )


def _layer_from_env(environ: dict[str, str]) -> ConnectionConfig:
    """Collect the connection settings set in the environment."""
    return ConnectionConfig(
        address=environ.get(ENV_ADDRESS),
        port=_env_int(environ, ENV_PORT),
        user=environ.get(ENV_USER),
        password=environ.get(ENV_PASSWORD),
        ssl=_env_bool(environ, ENV_SSL),
        namespace=environ.get(ENV_NAMESPACE),
    )


def _layer_from_file(config_file: str | None) -> ConnectionConfig:
    """Read the connection settings from a TM1py-style ini file.

    Raises ``FileNotFoundError`` when the file does not exist and
    ``ValueError`` when it has no ``[tm1]`` section.
    """
    if not config_file:
        return ConnectionConfig()
    parser = configparser.ConfigParser()
    with open(config_file, encoding="utf-8") as handle:
        parser.read_file(handle)
    if not parser.has_section(CONFIG_SECTION):
        raise ValueError(f"config file {config_file!r} has no [{CONFIG_SECTION}] section")
    section = parser[CONFIG_SECTION]
    ssl_raw = section.get("ssl")
    return ConnectionConfig(
        address=section.get("address"),
        port=section.getint("port", fallback=None),
        user=section.get("user"),
        password=section.get("password"),
        ssl=_parse_bool(ssl_raw, f"[{CONFIG_SECTION}] ssl in {config_file}") if ssl_raw else None,
        namespace=section.get("namespace"),
    )


def _env_int(environ: dict[str, str], name: str) -> int | None:
    """Read an integer from an environment variable, ``None`` when unset."""
    raw = environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"environment variable {name} must be an integer, got {raw!r}") from exc


def _env_bool(environ: dict[str, str], name: str) -> bool | None:
    """Read a boolean from an environment variable, ``None`` when unset."""
    raw = environ.get(name)
    if raw is None or raw == "":
        return None
    return _parse_bool(raw, name)


def _parse_bool(raw: str, setting: str) -> bool:
    """Parse a boolean from a string, raising a clear error otherwise."""
    normalized = raw.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    raise ValueError(f"{setting} must be one of {sorted(_TRUTHY | _FALSY)}, got {raw!r}")

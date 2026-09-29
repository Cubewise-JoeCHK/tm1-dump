"""Connection settings resolution shared by the dump and load engines.

Precedence: CLI arguments > environment variables (``TM1_ADDRESS``,
``TM1_PORT``, ``TM1_USER``, ``TM1_PASSWORD``, ``TM1_SSL``,
``TM1_NAMESPACE``) > config file (TM1py-style ini with a ``[tm1]``
section: the explicit ``--config-file``, or ``./config.ini`` when that
is absent).
"""

from __future__ import annotations

import argparse
import configparser
import os
from dataclasses import dataclass, fields

import requests

CONFIG_SECTION = "tm1"

#: Written by ``tm1-dump init`` and auto-discovered from the current directory.
DEFAULT_CONFIG_NAME = "config.ini"

#: TM1 servers default to SSL; ``--no-ssl`` / ``TM1_SSL=false`` / ini turn it off.
DEFAULT_SSL = True

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

    For each setting the first layer that provides a value wins. The config
    layer is the explicit ``--config-file``; without one, ``./config.ini`` is
    discovered from the current directory when it exists.
    """
    config_file = args.config_file or _discover_config_file()
    layers = (_layer_from_cli(args), _layer_from_env(os.environ), _layer_from_file(config_file))
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


def _discover_config_file() -> str | None:
    """Return ``./config.ini`` when it exists, ``None`` otherwise.

    Testable via ``monkeypatch.chdir(tmp_path)`` — the lookup is plain
    ``os.getcwd()``.
    """
    candidate = os.path.join(os.getcwd(), DEFAULT_CONFIG_NAME)
    return candidate if os.path.isfile(candidate) else None


def _layer_from_file(config_file: str | None) -> ConnectionConfig:
    """Read the connection settings from a TM1py-style ini file.

    Empty options count as unset, so a fresh ``tm1-dump init`` template
    resolves to nothing configured. Raises ``FileNotFoundError`` when the
    file does not exist and ``ValueError`` when it has no ``[tm1]`` section.
    """
    if not config_file:
        return ConnectionConfig()
    parser = configparser.ConfigParser()
    with open(config_file, encoding="utf-8") as handle:
        parser.read_file(handle)
    if not parser.has_section(CONFIG_SECTION):
        raise ValueError(f"config file {config_file!r} has no [{CONFIG_SECTION}] section")
    section = parser[CONFIG_SECTION]
    ssl_raw = _ini_value(section, "ssl")
    return ConnectionConfig(
        address=_ini_value(section, "address"),
        port=_ini_port(section, config_file),
        user=_ini_value(section, "user"),
        password=_ini_value(section, "password"),
        ssl=_parse_bool(ssl_raw, f"[{CONFIG_SECTION}] ssl in {config_file}") if ssl_raw else None,
        namespace=_ini_value(section, "namespace"),
    )


def _ini_value(section: configparser.SectionProxy, name: str) -> str | None:
    """Read an ini option, treating an empty or blank value as unset."""
    raw = section.get(name)
    return raw if raw and raw.strip() else None


def _ini_port(section: configparser.SectionProxy, config_file: str) -> int | None:
    """Read the ini port, ``None`` when unset, clear error when not an integer."""
    raw = _ini_value(section, "port")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"[{CONFIG_SECTION}] port in {config_file} must be an integer, got {raw!r}") from exc


def connection_problem(resolved: ConnectionConfig) -> str | None:
    """Return why the resolved connection cannot be used, or None.

    The remedy leads with ``tm1-dump init`` — the nothing-configured-anywhere
    case is the one new users hit.
    """
    missing = [
        flag
        for flag, value in (
            ("--address / TM1_ADDRESS", resolved.address),
            ("--port / TM1_PORT", resolved.port),
            ("--user / TM1_USER", resolved.user),
        )
        if value is None
    ]
    if missing:
        return (
            "no target server: missing "
            + ", ".join(missing)
            + " — run 'tm1-dump init' to create a config.ini, or pass CLI flags, "
            "set env vars, or use --config-file"
        )
    return None


#: Appended to the connection error when the failure is an SSL mismatch:
#: talking TLS to a server answering plain HTTP fails with e.g.
#: ``SSL: WRONG_VERSION_NUMBER`` and almost always means the target expects
#: ``ssl = false``.
SSL_HINT = "the server answered plain HTTP — set 'ssl = false' in config.ini or pass --no-ssl"


def describe_connection(resolved: ConnectionConfig, args: argparse.Namespace) -> str:
    """One-line diagnostic of the connection about to be opened; never the password.

    Format: ``target: <address>:<port> ssl=on|off user=<user> (config: <source>)``.
    ``ssl`` shows the effective setting (unset means the TM1 default, on);
    source is the config file path in play, else ``env``, ``cli`` or ``defaults``.
    """
    ssl_effective = resolved.ssl if resolved.ssl is not None else DEFAULT_SSL
    return (
        f"target: {resolved.address}:{resolved.port} ssl={'on' if ssl_effective else 'off'} "
        f"user={resolved.user} (config: {_config_source(args)})"
    )


def _config_source(args: argparse.Namespace) -> str:
    """Name where the connection configuration came from.

    The file path wins whenever a config file is in play — that is the case
    users need to see (an edited-in-the-wrong-directory ``config.ini``).
    Without a file, ``env`` when any environment variable provided a value,
    ``cli`` when any command-line option did, ``defaults`` when nothing
    anywhere did.
    """
    config_file = args.config_file or _discover_config_file()
    if config_file:
        return config_file
    if _layer_has_value(_layer_from_env(os.environ)):
        return "env"
    if _layer_has_value(_layer_from_cli(args)):
        return "cli"
    return "defaults"


def _layer_has_value(layer: ConnectionConfig) -> bool:
    """Whether the layer provides at least one connection setting."""
    return any(getattr(layer, setting.name) is not None for setting in fields(ConnectionConfig))


def connection_error_text(exc: BaseException) -> str:
    """Human-readable text for a failed connect; SSL mismatches gain a hint.

    The original error text always stays visible; the plain-HTTP hint is
    appended when the failure is — or chains to — a ``requests`` ``SSLError``.
    All other failures pass through untouched.
    """
    text = str(exc) or exc.__class__.__name__
    if _is_ssl_error(exc):
        return f"{text} — {SSL_HINT}"
    return text


def _is_ssl_error(exc: BaseException) -> bool:
    """Whether ``exc`` is, or was raised while handling, a requests SSLError.

    TM1py 2.x re-raises ``requests`` connection errors as-is, but through
    retry handlers that can interleave wrapper exceptions — walk the
    ``__cause__``/``__context__`` chain so the hint survives wrapping.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, requests.exceptions.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


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

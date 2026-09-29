"""Command-line interface for tm1-dump."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from tm1_dump.dump import run_dump
from tm1_dump.initcmd import run_init
from tm1_dump.load import run_load


def build_parser() -> argparse.ArgumentParser:
    """Build the ``tm1-dump`` parser with its ``init``, ``dump`` and ``load`` subcommands."""
    parser = argparse.ArgumentParser(
        prog="tm1-dump",
        description="Dump one TM1 instance to one zip — and reload it onto another server.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="write a starter config.ini in the current directory")
    init_parser.add_argument("--force", action="store_true", help="overwrite an existing config.ini")
    init_parser.set_defaults(func=run_init)

    dump_parser = subparsers.add_parser("dump", help="dump one TM1 instance into a zip archive")
    _add_connection_options(dump_parser)
    dump_parser.add_argument(
        "--include", action="append", metavar="TYPE=PATTERN", help="include only objects matching PATTERN; repeatable"
    )
    dump_parser.add_argument(
        "--exclude", action="append", metavar="TYPE=PATTERN", help="skip objects matching PATTERN; repeatable"
    )
    dump_parser.add_argument(
        "--no-data",
        action="store_true",
        help=(
            "skip regular cube data; keeps }ElementAttributes_* attribute values; "
            "combines on top of --include/--exclude (both must pass — with "
            '--exclude "data=..." the zip may carry no data files at all)'
        ),
    )
    dump_parser.add_argument("--workers", type=int, metavar="N", help="number of parallel workers")
    dump_parser.add_argument("--out", metavar="FILE.zip", help="output zip path")
    dump_parser.set_defaults(func=run_dump)

    load_parser = subparsers.add_parser("load", help="reload a dump zip onto a TM1 server")
    load_parser.add_argument("zip_file", metavar="ZIP", help="dump zip to load")
    _add_connection_options(load_parser)
    load_parser.add_argument("--workers", type=int, metavar="N", help="number of parallel workers")
    load_parser.add_argument(
        "--clean", action="store_true", help="delete matching objects on the target before loading"
    )
    load_parser.add_argument(
        "--dry-run", action="store_true", help="show what would be loaded without touching the server"
    )
    load_parser.set_defaults(func=run_load)

    return parser


def _add_connection_options(parser: argparse.ArgumentParser) -> None:
    """Add the shared TM1 connection options to a subcommand parser."""
    parser.add_argument("--address", help="TM1 admin host or IP")
    parser.add_argument("--port", type=int, help="TM1 HTTP API port")
    parser.add_argument("--user", help="TM1 user name")
    parser.add_argument("--password", help="TM1 password")
    parser.add_argument("--ssl", dest="ssl", action="store_true", default=None, help="use HTTPS")
    parser.add_argument("--no-ssl", dest="ssl", action="store_false", help="use HTTP")
    parser.add_argument("--namespace", help="CAM namespace (SAML/Cognos security mode)")
    parser.add_argument("--config-file", help="TM1py-style ini file with a [tm1] section (default: ./config.ini)")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch to the selected subcommand."""
    args = build_parser().parse_args(argv)
    return args.func(args)

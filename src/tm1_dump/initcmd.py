"""The ``tm1-dump init`` subcommand: write a starter ``config.ini``."""

from __future__ import annotations

import argparse
import os
import sys

CONFIG_NAME = "config.ini"

TEMPLATE = """\
# tm1-dump connection settings (TM1py-style ini).
# Read automatically from ./config.ini when you run tm1-dump in this directory;
# CLI flags and TM1_* environment variables still win over this file.

[tm1]
# TM1 admin host or IP (e.g. 192.168.1.10 or tm1.example.com)
address =
# TM1 HTTP API port (from the server's tm1s.cfg, e.g. 8010)
port =
# TM1 user name
user =
# TM1 password - stored in PLAINTEXT here, so keep this file private
password =
# HTTPS (the TM1 default). Uncomment to override; values: true / false.
# ssl = true
# CAM namespace for SAML/Cognos security mode. Leave commented for standard security.
# namespace =
"""


def run_init(args: argparse.Namespace) -> int:
    """Write a starter ``config.ini`` into the current directory."""
    target = os.path.join(os.getcwd(), CONFIG_NAME)
    if os.path.exists(target) and not args.force:
        print(f"tm1-dump: error: {target} already exists — pass --force to overwrite it", file=sys.stderr)
        return 1
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(TEMPLATE)
    print(f"wrote {target}")
    print("next: edit config.ini, then run: tm1-dump dump")
    return 0

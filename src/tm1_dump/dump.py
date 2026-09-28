"""Dump engine: write one TM1 instance into one zip archive.

Implemented in issue #2; the CLI dispatches here via :func:`run_dump`.
"""

from __future__ import annotations

import argparse


def run_dump(args: argparse.Namespace) -> int:
    """Dump one TM1 instance into the zip given by ``args.out``."""
    raise NotImplementedError("dump engine is not implemented yet (issue #2)")

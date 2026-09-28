"""Load engine: reload a tm1-dump zip onto a TM1 server.

Implemented in issue #3; the CLI dispatches here via :func:`run_load`.
"""

from __future__ import annotations

import argparse


def run_load(args: argparse.Namespace) -> int:
    """Load the dump zip ``args.zip_file`` onto the target server."""
    raise NotImplementedError("load engine is not implemented yet (issue #3)")

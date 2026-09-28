"""tm1-dump: dump one TM1 instance to one zip, and reload it elsewhere."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tm1-dump")
except PackageNotFoundError:  # pragma: no cover - package not installed
    __version__ = "0.0.0"

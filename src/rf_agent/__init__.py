"""The RF agent of sonoran-contributor: what a member's own receivers hear, sent to the broker."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sonoran-contributor")
except PackageNotFoundError:  # run from a source tree that is not installed
    __version__ = "0.0.0"

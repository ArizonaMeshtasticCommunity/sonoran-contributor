"""rf-agent: what a member's own receivers hear, sent to the community broker."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rf-agent")
except PackageNotFoundError:  # run from a source tree that is not installed
    __version__ = "0.0.0"

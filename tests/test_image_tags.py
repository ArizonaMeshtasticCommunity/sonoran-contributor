"""tools/image_tags.sh: the tags the Image workflow publishes for a push."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "image_tags.sh"
IMAGE = "ghcr.io/org/rf-agent"


def run(ref):
    return subprocess.run(["bash", str(SCRIPT), IMAGE, ref], capture_output=True, text=True)


def tags(ref):
    out = run(ref)
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


def refused(ref):
    out = run(ref)
    return out.returncode != 0 and out.stdout == ""


def test_main_publishes_main():
    assert tags("refs/heads/main") == [f"{IMAGE}:main"]


def test_a_branch_name_with_a_slash_is_a_valid_tag():
    assert tags("refs/heads/fix/thing") == [f"{IMAGE}:fix-thing"]


def test_a_release_publishes_the_version_its_minor_and_its_major():
    assert tags("refs/tags/v1.2.3") == [f"{IMAGE}:1.2.3", f"{IMAGE}:1.2", f"{IMAGE}:1"]


def test_a_zero_release_publishes_no_major():
    assert tags("refs/tags/v0.6.1") == [f"{IMAGE}:0.6.1", f"{IMAGE}:0.6"]


def test_a_prerelease_publishes_only_itself():
    assert tags("refs/tags/v1.2.3-rc.1") == [f"{IMAGE}:1.2.3-rc.1"]


@pytest.mark.parametrize(
    "ref",
    [
        "refs/tags/v1.2",
        "refs/tags/v1.2.3.4",
        "refs/tags/v01.2.3",
        "refs/tags/1.2.3",
        "refs/tags/vnext",
        "refs/pull/5/merge",
        "",
    ],
)
def test_anything_else_publishes_nothing(ref):
    assert refused(ref)

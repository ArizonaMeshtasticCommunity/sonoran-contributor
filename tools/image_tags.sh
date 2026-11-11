#!/usr/bin/env bash
# The image tags a push publishes, one `image:tag` per line.
#
#     tools/image_tags.sh ghcr.io/org/rf-agent refs/tags/v1.2.3
#
# The same rules docker/metadata-action applied before the workflow had
# to stop using it (.github/workflows/image.yml):
#
# - A branch publishes its name: `main`.
# - A tag `v1.2.3` publishes `1.2.3`, `1.2` and `1`, so a member who runs
#   `rf-agent:1` gets every compatible release. A `v0.x.y` tag publishes
#   no `0`, because 0.x releases promise no compatibility.
# - A prerelease tag `v1.2.3-rc.1` publishes only `1.2.3-rc.1`.
#
# Anything else is refused, so a mistyped tag publishes nothing.

set -euo pipefail

if (($# != 2)); then
  echo "usage: $0 <image> <git ref>" >&2
  exit 2
fi
image=$1
ref=$2

case "$ref" in
  refs/heads/*)
    branch=${ref#refs/heads/}
    # A tag cannot hold a slash.
    echo "$image:${branch//\//-}"
    ;;
  refs/tags/v*)
    version=${ref#refs/tags/v}
    if [[ ! $version =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$ ]]; then
      echo "not a version tag: $ref" >&2
      exit 1
    fi
    major=${BASH_REMATCH[1]}
    minor=${BASH_REMATCH[2]}
    prerelease=${BASH_REMATCH[4]}
    echo "$image:$version"
    if [[ -z $prerelease ]]; then
      echo "$image:$major.$minor"
      if [[ $major != 0 ]]; then
        echo "$image:$major"
      fi
    fi
    ;;
  *)
    echo "not a branch or a version tag: $ref" >&2
    exit 1
    ;;
esac

# syntax=docker/dockerfile:1

# The lock decides what ships.
#
# uv turns `uv.lock` into a fully pinned, hash-checked requirements file.
# This stage runs on the machine doing the build, whatever the target, so
# the image builds for every board members run, including 32-bit ARM,
# which the uv image does not ship for. `--frozen` makes a lock that has
# drifted from pyproject.toml a build failure, not a quiet re-resolve.
#
# uv is named by version and resolved by digest, as in Sonoran, so the
# tag cannot be moved underneath us. To update: change both halves
# together, in their own commit.
FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:0.10.1@sha256:452e02b117acd2d4eb3ba81a607bed9733b101b6c49492e352b1973463389012 AS lock
WORKDIR /src
COPY pyproject.toml uv.lock ./
# `--extra mesh`: the Meshtastic library, for mesh jobs (Sonoran #178).
RUN ["/uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--extra", "mesh", \
     "--format", "requirements-txt", "-o", "/requirements.txt"]

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# `--require-hashes` makes a substituted artefact a build failure rather
# than a silent install.
COPY --from=lock /requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt

# The project itself, and nothing else. `--no-deps` keeps the pinned
# install above authoritative.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps . && rm -rf /app/src

# No privileges, no listening ports. The contributor only connects out:
# to its decoders, to the member's own node, and to the broker over TLS.
RUN useradd --system --no-create-home --uid 10001 rf-agent
USER rf-agent

# Runs the RF agent when SOURCES is set, mesh jobs when MESH_NODE is set,
# or both. A station that ran `rf-agent` runs the same with this.
ENTRYPOINT ["sonoran-contributor"]

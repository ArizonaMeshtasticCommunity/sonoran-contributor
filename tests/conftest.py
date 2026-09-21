"""Shared helpers: the contract check, and replaying the captures."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(FIXTURES))

# `tools/validate.py`, used as it is: the same schema file and the same
# validator settings, so a message that passes here passes there.
_spec = importlib.util.spec_from_file_location("validate", ROOT / "tools" / "validate.py")
assert _spec is not None and _spec.loader is not None
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)

VALIDATOR = Draft202012Validator(
    json.loads(validate.SCHEMA.read_text()), format_checker=FormatChecker()
)

# The fictional site the captures fly round. See fixtures/make_captures.py.
SITE = (32.80, -113.30)
# The capture's 12 MHz counter starts at 0; replay it from here.
EPOCH = 1_790_100_000.0


def contract_errors(message: dict[str, Any]) -> list[str]:
    """Errors against the contract, after a JSON round trip as sent."""
    data = json.loads(json.dumps(message))
    return [e.message for e in VALIDATOR.iter_errors(data)]


def assert_valid(message: dict[str, Any]) -> None:
    errors = contract_errors(message)
    assert not errors, errors


def replay_beast(tracker, data: bytes) -> Iterator:
    """Feed a Beast capture frame by frame, at the times it was recorded."""
    for frame in tracker.framer.feed(data):
        point = tracker.frame(frame, EPOCH + frame.ticks / 12_000_000)
        if point is not None:
            yield point


@pytest.fixture
def beast_capture() -> bytes:
    return (FIXTURES / "beast.bin").read_bytes()


@pytest.fixture
def aircraft_snapshots() -> list[bytes]:
    return (FIXTURES / "aircraft.jsonl").read_bytes().splitlines()


@pytest.fixture
def dump978_capture() -> bytes:
    return (FIXTURES / "dump978.jsonl").read_bytes()

"""The mesh jobs a contributor does for Sonoran (Sonoran #178, #179).

The claims are from Sonoran's issues and docs/design/contributors.md
there: the contributor does only allowed kinds, ignores an expired job,
reports every job it starts, waits no longer than the Meshtastic
library's own timeout, and sends at most 250 heard nodes. Every message
must pass the contract copied from Sonoran.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from sonoran_contributor.contract import (
    TOPIC_ROOT,
    Job,
    inbox_topic,
    outbox_filter,
    parse_job,
    status_message,
    status_topic,
)
from sonoran_contributor.jobs import (
    JOB_WAIT_S,
    MAX_HEARD,
    LinkError,
    Reply,
    Seen,
    do_job,
    heard_list,
)

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = Draft202012Validator(
    json.loads((ROOT / "schema" / "contributor-message-v1.schema.json").read_text()),
    format_checker=FormatChecker(),
)
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
JOB_ID = "0123456789abcdef0123456789abcdef"


def assert_valid(message: dict[str, Any]) -> None:
    errors = [e.message for e in CONTRACT.iter_errors(json.loads(json.dumps(message)))]
    assert not errors, errors


def _job(**changes: Any) -> Job:
    body: dict[str, Any] = {
        "v": 1,
        "job": JOB_ID,
        "kind": "local_stats",
        "target": "!1234abcd",
        "issued": NOW.isoformat(),
        "expires": (NOW + timedelta(seconds=300)).isoformat(),
    }
    body.update(changes)
    job = parse_job(json.dumps(body).encode(), JOB_ID)
    assert job is not None
    return job


class FakeLink:
    """A node that answers, or not, and records what it was asked."""

    def __init__(self, reply: Reply | None = None, error: str | None = None) -> None:
        self.reply = reply
        self.error = error
        self.asked: list[tuple[int, float]] = []

    async def request_local_stats(self, target: int, wait_s: float) -> Reply | None:
        self.asked.append((target, wait_s))
        if self.error:
            raise LinkError(self.error)
        return self.reply


REPLY = Reply(
    local_stats={"noise_floor": -103, "channel_utilization": 11.0, "num_packets_rx": 5000},
    received=NOW + timedelta(seconds=40),
    hops=2,
    rx_snr=-7.5,
    rx_rssi=-118,
)


async def _do(job: Job, link: FakeLink, allow: frozenset[str] = frozenset({"local_stats"})):
    return await do_job(job, link, allow, "phoenix", "pi5", lambda: NOW)


# --- Topics ------------------------------------------------------------


def test_topics_are_sonorans() -> None:
    assert TOPIC_ROOT == "sonoran/v1/contributors"
    assert outbox_filter("phoenix", "pi5") == "sonoran/v1/contributors/phoenix/pi5/outbox/+"
    assert inbox_topic("phoenix", "pi5", JOB_ID).endswith(f"/phoenix/pi5/inbox/{JOB_ID}")
    assert status_topic("phoenix", "pi5") == "sonoran/v1/contributors/phoenix/pi5/status"


# --- Reading a job -----------------------------------------------------


def test_an_empty_retained_message_is_a_cleared_job() -> None:
    assert parse_job(b"", JOB_ID) is None


def test_a_job_whose_topic_and_body_disagree_is_refused() -> None:
    body = json.dumps({**_job().as_dict(), "job": "f" * 32}).encode()
    with pytest.raises(ValueError):
        parse_job(body, JOB_ID)


def test_a_bad_target_is_refused() -> None:
    with pytest.raises(ValueError):
        _job(target="1234abcd")


# --- Doing a job (#179) ------------------------------------------------


async def test_an_answer_becomes_an_ok_result() -> None:
    link = FakeLink(REPLY)
    result = await _do(_job(), link)
    assert result is not None
    assert result["outcome"] == "ok"
    assert result["local_stats"]["noise_floor"] == -103
    assert result["hops"] == 2
    assert link.asked == [(0x1234ABCD, 300.0)]
    assert_valid(result)


async def test_no_answer_is_reported_as_no_reply() -> None:
    """Every job it starts gets a result, so Sonoran can move on at once."""
    result = await _do(_job(), FakeLink(None))
    assert result is not None
    assert result["outcome"] == "no_reply"
    assert_valid(result)


async def test_a_node_error_is_reported() -> None:
    result = await _do(_job(), FakeLink(error="NO_RESPONSE"))
    assert result is not None
    assert (result["outcome"], result["detail"]) == ("error", "NO_RESPONSE")
    assert_valid(result)


async def test_a_kind_not_allowed_is_refused_and_the_node_is_not_used() -> None:
    link = FakeLink(REPLY)
    result = await _do(_job(), link, allow=frozenset())
    assert result is not None
    assert result["outcome"] == "refused"
    assert link.asked == []
    assert_valid(result)


async def test_an_expired_job_is_ignored() -> None:
    link = FakeLink(REPLY)
    job = _job(expires=(NOW - timedelta(seconds=1)).isoformat())
    assert await _do(job, link) is None
    assert link.asked == []


async def test_the_wait_never_passes_the_jobs_expiry() -> None:
    link = FakeLink(None)
    await _do(_job(expires=(NOW + timedelta(seconds=90)).isoformat()), link)
    assert link.asked == [(0x1234ABCD, 90.0)]


def test_the_wait_is_the_meshtastic_library_timeout() -> None:
    """MeshInterface(timeout=300), meshtastic/mesh_interface.py at 2.7.11."""
    assert JOB_WAIT_S == 300.0


async def test_a_reply_stamped_after_now_is_not_sent_from_the_future() -> None:
    late = Reply(local_stats={"noise_floor": -100}, received=NOW + timedelta(hours=1))
    result = await _do(_job(), FakeLink(late))
    assert result is not None
    assert result["received"] == result["sent"]


async def test_unknown_local_stats_fields_are_dropped() -> None:
    odd = Reply(local_stats={"noise_floor": -100, "something_new": 1}, received=NOW)
    result = await _do(_job(), FakeLink(odd))
    assert result is not None
    assert "something_new" not in result["local_stats"]


# --- Retained jobs arrive again on every reconnect ---------------------


def test_a_job_is_done_once() -> None:
    seen = Seen()
    assert seen.first(JOB_ID)
    assert not seen.first(JOB_ID)


def test_seen_forgets_the_oldest_beyond_its_limit() -> None:
    seen = Seen(limit=2)
    for job in ("a" * 32, "b" * 32, "c" * 32):
        seen.first(job)
    assert seen.first("a" * 32)


# --- The heartbeat -----------------------------------------------------


def test_the_heard_list_is_the_most_recent_250() -> None:
    """250 = MAX_NUM_NODES on an ESP32-S3 with 16 MB flash (firmware)."""
    assert MAX_HEARD == 250
    base = NOW.timestamp()
    nodes = [(n, 1, base - n) for n in range(1, 400)]
    heard = heard_list(nodes, NOW)
    assert len(heard) == 250
    assert heard[0]["node"] == "!00000001"
    assert heard[-1]["node"] == f"!{250:08x}"


def test_a_node_never_heard_is_left_out() -> None:
    assert heard_list([(1, None, None), (2, 0, NOW.timestamp())], NOW) == [
        {"node": "!00000002", "hops": 0, "last_heard": NOW.isoformat()}
    ]


def test_the_status_passes_the_contract() -> None:
    message = status_message(
        "phoenix",
        "pi5",
        NOW,
        "0.6.0",
        0xA1B2C3D4,
        frozenset({"local_stats"}),
        heard_list([(2, 1, NOW.timestamp())], NOW),
    )
    assert message["mesh_node"] == "!a1b2c3d4"
    assert_valid(message)

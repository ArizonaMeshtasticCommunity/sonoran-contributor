"""rf-registrar: locations, their sources, and their broker logins (Sonoran #305)."""

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rf_registrar.app import make_app
from rf_registrar.broker import BrokerError, rules_for
from rf_registrar.config import Config, ConfigError, load
from rf_registrar.service import (
    Actor,
    Conflict,
    Forbidden,
    Invalid,
    Limited,
    NotFound,
    Registrar,
    clean_name,
)
from rf_registrar.store import Store

from conftest import assert_valid

KEY = "k" * 40
OWNER = Actor("111", "Logan")
OTHER = Actor("222", "Will")
OPERATOR = Actor("999", "Op", operator=True)


class FakeBroker:
    def __init__(self):
        self.users: dict[str, str] = {}
        self.rules: dict[str, list] = {}
        self.retained: dict[str, bytes] = {}
        self.kicked: list[str] = []
        self.fail: set[str] = set()
        self.guarded = False

    def _maybe_fail(self, name):
        if name in self.fail:
            raise BrokerError(name)

    async def create_user(self, username, password):
        self._maybe_fail("create_user")
        assert username not in self.users
        self.users[username] = password

    async def set_password(self, username, password):
        self._maybe_fail("set_password")
        self.users[username] = password

    async def delete_user(self, username):
        self.users.pop(username, None)

    async def set_rules(self, username, rules):
        self._maybe_fail("set_rules")
        self.rules[username] = rules

    async def delete_rules(self, username):
        self.rules.pop(username, None)

    async def kick(self, username):
        self.kicked.append(username)
        return 1

    async def publish_retained(self, topic, payload):
        self._maybe_fail("publish_retained")
        self.retained[topic] = payload

    async def guard_registry(self):
        self._maybe_fail("guard_registry")
        added = not self.guarded
        self.guarded = True
        return added


def config(**changes) -> Config:
    base = {
        "api_key": KEY,
        "emqx_url": "http://broker.example/api/v5",
        "emqx_key": "k",
        "emqx_secret": "s",
        "db_path": ":memory:",
        "max_locations": 2,
        "max_sources": 3,
        "mints_per_hour": 4,
        "reserved": frozenset({"old-site"}),
    }
    return Config(**{**base, **changes})


class Clock:
    def __init__(self):
        self.now = 1_790_100_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def reg(broker, clock):
    counter = iter(range(1000))
    return Registrar(
        config(), Store(":memory:"), broker, clock=clock, password=lambda: f"pw{next(counter)}"
    )


def registry(broker, station):
    payload = broker.retained[f"rf/v1/{station}/registry"]
    return json.loads(payload) if payload else None


async def test_an_owner_makes_a_location_and_two_sources(reg, broker):
    """The 'done when' of #305, up to the broker."""
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    adsb = await reg.mint("tempe-roof", "adsb-pi", OWNER)
    sweep = await reg.mint("tempe-roof", "sweeper", OWNER)

    assert (adsb.username, adsb.station, adsb.source) == (
        "rf-tempe-roof-adsb-pi",
        "tempe-roof",
        "adsb-pi",
    )
    assert broker.users == {"rf-tempe-roof-adsb-pi": "pw0", "rf-tempe-roof-sweeper": "pw1"}
    assert broker.rules["rf-tempe-roof-sweeper"] == rules_for("tempe-roof", "sweeper")
    assert sweep.password == "pw1"

    entry = registry(broker, "tempe-roof")
    assert_valid(entry)
    assert entry["display_name"] == "Tempe roof" and entry["owner"] == "Logan"
    assert entry["sources"] == ["adsb-pi", "sweeper"]
    assert "111" not in json.dumps(entry), "the registry never carries an account id"


def test_a_sources_rules_allow_its_own_topics_only():
    """First match decides. The order is the whole of the security."""

    def decide(topic, action="publish"):
        for rule in rules_for("tempe-roof", "adsb-pi"):
            if rule["action"] not in (action, "all"):
                continue
            pattern = rule["topic"].split("/")
            parts = topic.split("/")
            ok = True
            for i, word in enumerate(pattern):
                if word == "#":
                    break
                if i >= len(parts) or (word != "+" and word != parts[i]):
                    ok = False
                    break
            else:
                ok = len(parts) == len(pattern)
            if ok:
                return rule["permission"]
        return "no match"

    assert decide("rf/v1/tempe-roof/adsb") == "allow"
    assert decide("rf/v1/tempe-roof/spectrum") == "allow"
    assert decide("rf/v1/tempe-roof/status/adsb-pi") == "allow"
    assert decide("rf/v1/tempe-roof/status/sweeper") == "deny"
    assert decide("rf/v1/tempe-roof/status") == "deny"
    assert decide("rf/v1/tempe-roof/registry") == "deny"
    assert decide("rf/v1/other-site/adsb") == "deny"
    assert decide("msh/US/AZ/2/e/LongFast/!a1b2c3d4") == "deny"
    assert decide("rf/v1/#", "subscribe") == "deny"


async def test_only_the_owner_or_an_operator_may_change_a_location(reg):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    for call in (
        reg.mint("tempe-roof", "x", OTHER),
        reg.rename("tempe-roof", "Mine now", OTHER),
        reg.transfer("tempe-roof", "222", "Will", OTHER),
        reg.delete("tempe-roof", OTHER),
    ):
        with pytest.raises(Forbidden):
            await call
    creds = await reg.mint("tempe-roof", "x", OPERATOR)
    assert creds.username == "rf-tempe-roof-x"


async def test_a_location_id_is_unique_and_a_reserved_one_needs_an_operator(reg):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    with pytest.raises(Conflict):
        await reg.create("Tempe-Roof", "Again", OTHER)
    with pytest.raises(Forbidden):
        await reg.create("old-site", "Old", OWNER)
    await reg.create("old-site", "Old", OPERATOR)


@pytest.mark.parametrize("bad", ["ab", "Bad Name", "x" * 33, "a/b", "", None])
async def test_a_bad_location_id_is_refused(reg, bad):
    with pytest.raises(Invalid):
        await reg.create(bad, "Name", OWNER)


@pytest.mark.parametrize("bad", ["-x", "Bad Source", "a" * 17, "a/b", "", None])
async def test_a_bad_source_name_is_refused(reg, bad):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    with pytest.raises(Invalid):
        await reg.mint("tempe-roof", bad, OWNER)


def test_display_names_are_cleaned():
    # Control and format characters go, including a zero-width space.
    assert clean_name("  Tempe\n roof\u200b \x07 ", "n") == "Tempe roof"
    with pytest.raises(Invalid):
        clean_name("\n\t", "n")
    with pytest.raises(Invalid):
        clean_name("x" * 65, "n")


async def test_members_are_limited_and_operators_are_not(reg, clock):
    await reg.create("site-one", "One", OWNER)
    await reg.create("site-two", "Two", OWNER)
    with pytest.raises(Limited):
        await reg.create("site-three", "Three", OWNER)

    for label in ("a", "b", "c"):
        await reg.mint("site-one", label, OWNER)
    with pytest.raises(Limited, match="sources"):
        await reg.mint("site-one", "d", OWNER)

    # Three mints so far; one more mint or rotation, then the hour is full.
    await reg.rotate("site-one", "a", OWNER)
    with pytest.raises(Limited, match="an hour"):
        await reg.mint("site-two", "e", OWNER)
    await reg.mint("site-two", "e", OPERATOR)
    clock.now += 3601
    await reg.mint("site-two", "f", OWNER)


async def test_a_second_mint_of_one_source_is_refused(reg):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    await reg.mint("tempe-roof", "adsb-pi", OWNER)
    with pytest.raises(Conflict, match="rotate"):
        await reg.mint("tempe-roof", "adsb-pi", OWNER)


async def test_rotate_changes_the_password_and_disconnects_the_pi(reg, broker):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    first = await reg.mint("tempe-roof", "adsb-pi", OWNER)
    second = await reg.rotate("tempe-roof", "adsb-pi", OWNER)
    assert second.password != first.password
    assert broker.users["rf-tempe-roof-adsb-pi"] == second.password
    assert broker.kicked == ["rf-tempe-roof-adsb-pi"]


async def test_revoking_one_source_leaves_the_other(reg, broker):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    await reg.mint("tempe-roof", "adsb-pi", OWNER)
    await reg.mint("tempe-roof", "sweeper", OWNER)
    await reg.revoke("tempe-roof", "adsb-pi", OWNER)

    assert list(broker.users) == ["rf-tempe-roof-sweeper"]
    assert list(broker.rules) == ["rf-tempe-roof-sweeper"]
    assert "rf-tempe-roof-adsb-pi" in broker.kicked
    assert registry(broker, "tempe-roof")["sources"] == ["sweeper"]
    with pytest.raises(NotFound):
        await reg.revoke("tempe-roof", "adsb-pi", OWNER)


async def test_a_login_whose_rules_fail_is_taken_away_again(reg, broker):
    """A login without its rules would fall through to the broker's defaults."""
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    broker.fail.add("set_rules")
    with pytest.raises(Exception, match="rules"):
        await reg.mint("tempe-roof", "adsb-pi", OWNER)
    assert broker.users == {}
    assert reg.store.sources("tempe-roof") == []


async def test_a_lost_registry_publish_does_not_undo_the_login(reg, broker):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    broker.fail.add("publish_retained")
    creds = await reg.mint("tempe-roof", "adsb-pi", OWNER)
    assert creds.username in broker.users
    broker.fail.clear()
    await reg.republish_all()
    assert registry(broker, "tempe-roof")["sources"] == ["adsb-pi"]


async def test_deleting_a_location_revokes_everything_and_clears_its_entry(reg, broker):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    await reg.mint("tempe-roof", "adsb-pi", OWNER)
    await reg.delete("tempe-roof", OWNER)
    assert broker.users == {} and broker.rules == {}
    assert broker.retained["rf/v1/tempe-roof/registry"] == b""
    with pytest.raises(NotFound):
        reg.get("tempe-roof")


async def test_transfer_and_rename_change_the_registry(reg, broker):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    await reg.transfer("tempe-roof", "222", "Will", OWNER)
    await reg.rename("tempe-roof", "Tempe roof east", OTHER)
    entry = registry(broker, "tempe-roof")
    assert (entry["owner"], entry["display_name"]) == ("Will", "Tempe roof east")
    with pytest.raises(Forbidden):
        await reg.rename("tempe-roof", "Back", OWNER)


async def test_every_change_is_logged(reg):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    await reg.mint("tempe-roof", "adsb-pi", OWNER)
    await reg.rotate("tempe-roof", "adsb-pi", OWNER)
    await reg.revoke("tempe-roof", "adsb-pi", OWNER)
    actions = [row[0] for row in reg.store.db.execute("SELECT action FROM event ORDER BY rowid")]
    assert actions == ["create", "mint", "rotate", "revoke"]
    # No password is stored anywhere.
    dump = "\n".join(reg.store.db.iterdump())
    assert "pw0" not in dump and "pw1" not in dump


def test_settings_are_checked_at_start():
    good = {
        "REGISTRAR_API_KEY": KEY,
        "EMQX_API_URL": "http://broker.example:18083/api/v5/",
        "EMQX_API_KEY": "k",
        "EMQX_API_SECRET": "s",
        "RESERVED_LOCATIONS": "Old-Site, other-site",
    }
    loaded = load(good)
    assert loaded.emqx_url == "http://broker.example:18083/api/v5"
    assert loaded.reserved == {"old-site", "other-site"}
    with pytest.raises(ConfigError) as caught:
        load({"REGISTRAR_API_KEY": "short", "RESERVED_LOCATIONS": "bad id"})
    for needle in ("REGISTRAR_API_KEY", "EMQX_API_URL", "EMQX_API_KEY", "RESERVED_LOCATIONS"):
        assert needle in str(caught.value)


async def test_the_http_api_needs_the_key_and_maps_errors(reg):
    async with TestClient(TestServer(make_app(config(), reg))) as client:
        assert (await client.get("/v1/health")).status == 200
        assert (await client.get("/v1/locations")).status == 401
        wrong = {"Authorization": "Bearer " + "x" * 40}
        assert (await client.get("/v1/locations", headers=wrong)).status == 401

        auth = {"Authorization": f"Bearer {KEY}"}
        owner = {"id": "111", "name": "Logan"}
        made = await client.post(
            "/v1/locations",
            json={"id": "tempe-roof", "display_name": "Tempe roof", "actor": owner},
            headers=auth,
        )
        assert made.status == 201
        minted = await client.post(
            "/v1/locations/tempe-roof/sources",
            json={"source": "adsb-pi", "actor": owner},
            headers=auth,
        )
        assert minted.status == 201
        assert (await minted.json())["username"] == "rf-tempe-roof-adsb-pi"

        theirs = await client.post(
            "/v1/locations/tempe-roof/sources",
            json={"source": "x", "actor": {"id": "222", "name": "Will"}},
            headers=auth,
        )
        assert theirs.status == 403
        # A claim to be an operator is the bot's to make, and it must be `true`.
        sneaky = {"id": "222", "name": "Will", "operator": "yes"}
        refused = await client.post(
            "/v1/locations/tempe-roof/sources/adsb-pi/revoke",
            json={"actor": sneaky},
            headers=auth,
        )
        assert refused.status == 403
        no_actor = await client.post(
            "/v1/locations/tempe-roof/rename", json={"display_name": "x"}, headers=auth
        )
        assert no_actor.status == 422
        missing = await client.get("/v1/locations/nowhere", headers=auth)
        assert missing.status == 404

        mine = await client.get("/v1/locations?owner=111", headers=auth)
        (row,) = await mine.json()
        assert [s["source"] for s in row["sources"]] == ["adsb-pi"]
        assert "password" not in json.dumps(row)


async def test_start_guards_the_registry_and_survives_a_broker_that_refuses(reg, broker):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    broker.retained.clear()
    broker.fail.add("guard_registry")
    await reg.start()
    assert not broker.guarded
    assert registry(broker, "tempe-roof")["display_name"] == "Tempe roof"
    broker.fail.clear()
    await reg.start()
    assert broker.guarded


async def test_no_location_passes_the_hard_source_cap(broker, clock):
    """Sonoran refuses a registry entry with more than 32 sources."""
    big = Registrar(config(max_sources=100), Store(":memory:"), broker, clock=clock)
    await big.create("big-site", "Big", OPERATOR)
    for i in range(32):
        await big.mint("big-site", f"pi{i}", OPERATOR)
    with pytest.raises(Limited, match="32"):
        await big.mint("big-site", "pi32", OPERATOR)
    assert_valid(registry(broker, "big-site"))


async def test_a_transfer_respects_the_new_owners_limit(reg):
    await reg.create("site-one", "One", OTHER)
    await reg.create("site-two", "Two", OTHER)
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    with pytest.raises(Limited, match="new owner"):
        await reg.transfer("tempe-roof", "222", "Will", OWNER)
    await reg.transfer("tempe-roof", "222", "Will", OPERATOR)


async def test_revoking_clears_the_pis_retained_heartbeat(reg, broker):
    """Sonoran reads retained heartbeats again on reconnect. A revoked Pi's
    must be gone, or it is listed again after every ingest restart."""
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    await reg.mint("tempe-roof", "adsb-pi", OWNER)
    broker.retained["rf/v1/tempe-roof/status/adsb-pi"] = b'{"v":1}'
    await reg.revoke("tempe-roof", "adsb-pi", OWNER)
    assert broker.retained["rf/v1/tempe-roof/status/adsb-pi"] == b""


async def test_deleting_a_location_clears_every_heartbeat_and_the_registry(reg, broker):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    await reg.mint("tempe-roof", "adsb-pi", OWNER)
    await reg.mint("tempe-roof", "sweeper", OWNER)
    await reg.delete("tempe-roof", OWNER)
    for topic in (
        "rf/v1/tempe-roof/status/adsb-pi",
        "rf/v1/tempe-roof/status/sweeper",
        "rf/v1/tempe-roof/status",
        "rf/v1/tempe-roof/registry",
    ):
        assert broker.retained[topic] == b"", topic


async def test_a_heartbeat_that_cannot_be_cleared_does_not_undo_the_revoke(reg, broker):
    await reg.create("tempe-roof", "Tempe roof", OWNER)
    await reg.mint("tempe-roof", "adsb-pi", OWNER)
    broker.fail.add("publish_retained")
    await reg.revoke("tempe-roof", "adsb-pi", OWNER)
    assert "rf-tempe-roof-adsb-pi" not in broker.users

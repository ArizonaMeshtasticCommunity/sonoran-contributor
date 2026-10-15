"""Alerts to a member's Discord webhook: a change is sent, a retry is not."""

import logging

import pytest

from rf_agent.alerts import MAX_CHARS, Alerter
from rf_agent.config import ConfigError, load
from rf_agent.quiet import Condition, set_notifier
from rf_agent.spectrum import EMPTY_MINUTES, SpectrumSummary

HOOK = "https://discord.com/api/webhooks/123/secret-token-value"
BASE = {
    "STATION_ID": "test-site",
    "BROKER_URL": "mqtts://broker.example",
    "SOURCES": "adsb=beast://readsb",
}


@pytest.fixture
def sent():
    alerts: list[str] = []
    set_notifier(alerts.append)
    yield alerts
    set_notifier(None)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_only_a_start_a_change_and_a_recovery_alert(sent):
    clock = Clock()
    down = Condition(logging.getLogger("t"), "adsb=beast://readsb:30005", clock=clock)
    for _ in range(500):
        down.bad("Connection refused")
        clock.now += 60  # hourly reminders happen in the log, not here
    down.bad("No route to host")
    down.ok("reading again")
    assert sent == [
        "🔴 adsb=beast://readsb:30005: Connection refused",
        "🔴 adsb=beast://readsb:30005: No route to host",
        "✅ adsb=beast://readsb:30005: reading again after 8 h 20 min",
    ]


def test_a_condition_marked_quiet_never_alerts(sent):
    down = Condition(logging.getLogger("t"), "alert webhook", alert=False)
    down.bad("the webhook answered 404")
    down.ok()
    assert sent == []


async def test_alerts_close_together_go_out_as_one_message():
    posts = []
    alerter = Alerter(HOOK, "test-site/pi", post=lambda url, body: posts.append((url, body)))
    alerter.add("🔴 broker: Connection refused")
    alerter.add("🔴 adsb=beast://readsb: Connection refused")
    await alerter.flush()
    await alerter.flush()  # nothing new: nothing sent
    ((url, body),) = posts
    assert url == HOOK
    assert body["content"].startswith("**sonoran-contributor test-site/pi**\n🔴 broker")
    assert "adsb=beast" in body["content"]
    assert body["allowed_mentions"] == {"parse": []}


async def test_a_webhook_that_is_down_keeps_the_alerts_and_never_logs_its_url(caplog):
    calls = 0

    def failing(url, body):
        nonlocal calls
        calls += 1
        raise OSError("the webhook cannot be reached: Name or service not known")

    alerter = Alerter(HOOK, "test-site", post=failing)
    alerter.add("🔴 spectrum: no sweeps")
    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            assert await alerter.flush() < 0
    assert calls == 5 and list(alerter.queue) == ["🔴 spectrum: no sweeps"]
    assert sum("webhook cannot be reached" in r.getMessage() for r in caplog.records) == 1
    assert "secret-token-value" not in caplog.text

    ok = []
    alerter.post = lambda url, body: ok.append(body)
    await alerter.flush()
    assert len(ok) == 1 and not alerter.queue


async def test_discord_asking_to_wait_is_honoured():
    alerter = Alerter(HOOK, "test-site", post=lambda url, body: 2.5)
    alerter.add("🔴 broker: down")
    assert await alerter.flush() == 2.5
    assert list(alerter.queue) == ["🔴 broker: down"], "kept for the next try"


def test_a_long_burst_fits_one_discord_message():
    alerter = Alerter(HOOK, "test-site")
    for i in range(80):
        alerter.add(f"🔴 source-{i}: " + "x" * 60)
    text = alerter.message()
    assert len(text) <= MAX_CHARS + 1
    assert "older alerts dropped" in text, "the queue holds 50; the rest are counted"


def test_the_setting_is_checked_and_never_echoed():
    assert load(BASE).alert_webhook is None
    assert load({**BASE, "ALERT_WEBHOOK": HOOK}).alert_webhook == HOOK
    with pytest.raises(ConfigError) as caught:
        load({**BASE, "ALERT_WEBHOOK": "http://discord.com/api/webhooks/1/leaky-token"})
    assert "ALERT_WEBHOOK" in str(caught.value)
    assert "leaky-token" not in str(caught.value)
    assert "secret-token-value" not in repr(load({**BASE, "ALERT_WEBHOOK": HOOK}))


def test_no_sweeps_for_a_few_minutes_is_an_alert_and_their_return_another(sent):
    """rf-sweep stays up with the dongle unplugged: the agent hears silence."""
    summary = SpectrumSummary("test-site")
    summary.feed(b"# rf-sweep range=902000000:928000000:50000 gain_db=32.8\n", 0.0)
    for minute in range(1, EMPTY_MINUTES + 3):
        summary.tick(minute * 60.0)
    assert len(sent) == 1 and "is the dongle plugged in?" in sent[0]
    t = (EMPTY_MINUTES + 3) * 60.0 + 1
    line = "2026-09-22, 18:00:00, 902000000, 903953125, 48828.12, 3000, -50.1, -50.2"
    summary.feed((line + "\n").encode(), t)
    summary.tick(t + 60)
    assert len(sent) == 2 and sent[1].startswith("✅ spectrum: fresh sweeps again")


def test_post_webhook_against_a_server_that_answers_like_discord():
    import http.server
    import json
    import threading

    from rf_agent.alerts import post_webhook

    answers = [(204, b""), (429, b'{"retry_after": 1.5}'), (404, b'{"message": "Unknown Webhook"}')]
    got = []

    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            code, body = answers.pop(0)
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/api/webhooks/1/secret-token-value"
    try:
        assert post_webhook(url, {"content": "hi"}) is None
        assert post_webhook(url, {"content": "hi"}) == 1.5
        with pytest.raises(OSError) as caught:
            post_webhook(url, {"content": "hi"})
        assert "404" in str(caught.value) and "secret-token-value" not in str(caught.value)
    finally:
        server.shutdown()
    assert got[0] == {"content": "hi"}


def test_an_error_that_flaps_between_two_texts_is_not_news_each_time(sent, caplog):
    """Refused, then no answer, then refused: before, every switch was logged."""
    clock = Clock()
    down = Condition(logging.getLogger("t.flap"), "adsb", clock=clock)
    with caplog.at_level(logging.WARNING, logger="t.flap"):
        for i in range(200):
            down.bad("Connection refused" if i % 2 else "no answer from readsb:30005")
            clock.now += 30
    assert len(caplog.records) == 2, [r.getMessage() for r in caplog.records]
    assert sent == [
        "🔴 adsb: no answer from readsb:30005",
        "🔴 adsb: Connection refused",
    ]


def test_a_short_blip_sends_nothing_to_discord(sent, caplog):
    """A decoder restarting for ten seconds is in the log, not in Discord."""
    clock = Clock()
    down = Condition(logging.getLogger("t.blip"), "uat", clock=clock)
    with caplog.at_level(logging.INFO, logger="t.blip"):
        down.bad("Connection refused")
        clock.now += 10
        down.bad("Connection refused")
        down.ok("reading again")
    assert sent == []
    assert [r.levelname for r in caplog.records] == ["WARNING", "INFO"], "the log still has it"


def test_an_alert_waits_two_minutes_then_its_recovery_follows(sent):
    clock = Clock()
    down = Condition(logging.getLogger("t"), "broker", clock=clock)
    down.bad("Connection refused")
    clock.now += 60
    down.bad("Connection refused")
    assert sent == []
    clock.now += 60
    down.bad("Connection refused")
    assert sent == ["🔴 broker: Connection refused"]
    clock.now += 600
    down.ok("connected again")
    assert sent[-1] == "✅ broker: connected again after 12 min"
    # A new outage starts afresh: its errors are news again.
    down.bad("Connection refused")
    clock.now += 120
    down.bad("Connection refused")
    assert sent[-1] == "🔴 broker: Connection refused" and len(sent) == 3

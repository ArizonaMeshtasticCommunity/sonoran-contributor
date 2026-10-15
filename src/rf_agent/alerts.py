"""Station alerts to the member's own Discord, through a webhook.

Optional: only with `ALERT_WEBHOOK` set. Each `quiet.Condition` sends
its start, any change and its recovery here: a decoder that stops
answering, the broker out of reach, a dongle that stalled or sends no
sweeps. Never one per retry, and never the hourly reminder.

Alerts that arrive close together go out as one message. A webhook that
cannot be reached keeps the newest alerts and tries again with backoff.
Its own trouble is logged, quietly, and never alerted: that would loop.

The webhook URL holds a token. It is never logged.

A Pi that loses power or its network cannot send its own alert. Watching
for a station that has gone silent is the server's job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable

from rf_agent.quiet import Condition

logger = logging.getLogger(__name__)

FLUSH_S = 5.0
MAX_QUEUED = 50
# Discord allows 2000 characters in a message.
MAX_CHARS = 1900
BACKOFF_MAX_S = 300.0

Post = Callable[[str, dict[str, object]], float | None]


def post_webhook(url: str, payload: dict[str, object]) -> float | None:
    """POST one message. Returns seconds to wait when Discord asks, else None.

    Raises OSError (urllib's errors included) on any other failure.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "sonoran-contributor"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            return None
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            try:
                return float(json.loads(exc.read() or b"{}").get("retry_after", 5))
            except (ValueError, AttributeError):
                return 5.0
        # Say what Discord answered, never the URL it was sent to.
        raise OSError(f"the webhook answered {exc.code}") from None
    except urllib.error.URLError as exc:
        raise OSError(f"the webhook cannot be reached: {exc.reason}") from None


class Alerter:
    def __init__(
        self,
        url: str,
        label: str,
        post: Post = post_webhook,
        flush_s: float = FLUSH_S,
    ) -> None:
        self.url = url
        self.label = label
        self.post = post
        self.flush_s = flush_s
        self.queue: deque[str] = deque(maxlen=MAX_QUEUED)
        self.dropped = 0
        self.down = Condition(logger, "alert webhook", alert=False)

    def add(self, text: str) -> None:
        if len(self.queue) == self.queue.maxlen:
            self.dropped += 1
        self.queue.append(text)

    def message(self) -> str:
        lines = list(self.queue)
        if self.dropped:
            lines.insert(0, f"({self.dropped} older alerts dropped while Discord was away)")
        head = f"**sonoran-contributor {self.label}**\n"
        body = ""
        for line in lines:
            if len(head) + len(body) + len(line) + 1 > MAX_CHARS:
                body += "…"
                break
            body += line + "\n"
        return head + body

    async def flush(self) -> float:
        """Send what is queued. Returns how long to wait before the next try."""
        if not self.queue:
            return self.flush_s
        sent = len(self.queue)
        payload = {"content": self.message(), "allowed_mentions": {"parse": []}}
        try:
            wait = await asyncio.to_thread(self.post, self.url, payload)
        except OSError as exc:
            self.down.bad(str(exc))
            return -1.0
        if wait is not None:
            return wait
        for _ in range(min(sent, len(self.queue))):
            self.queue.popleft()
        self.dropped = 0
        self.down.ok("sending alerts again")
        return self.flush_s

    async def run(self) -> None:
        backoff = self.flush_s
        while True:
            wait = await self.flush()
            if wait < 0:
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                wait = backoff
            else:
                backoff = self.flush_s
            await asyncio.sleep(wait)

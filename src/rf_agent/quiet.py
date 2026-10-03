"""Log a condition when it changes, not every time it is seen.

A station runs for months on a small board, often logging to syslog or
an SD card. A decoder that is down, or a dongle that is unplugged, is
retried for as long as it lasts. Logging every retry fills the disk with
one line repeated (2026-10-03). So each thing that can fail has a
`Condition`:

- The first failure is logged, as a warning.
- The same failure again is not logged. It is counted.
- A different failure is logged, because something changed.
- Once an hour, a failure that continues is logged again, with how
  many times it was seen and since when, so a long outage is still
  visible in a log that is read later.
- Recovery is logged once, as info, with how long it lasted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

REMIND_S = 3600.0


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


class Condition:
    """One thing that can be wrong, such as a decoder that does not answer."""

    def __init__(
        self,
        logger: logging.Logger,
        name: str,
        remind_s: float = REMIND_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.logger = logger
        self.name = name
        self.remind_s = remind_s
        self.clock = clock
        self.failing = False
        self.since = 0.0
        self.text = ""
        self.count = 0
        self.logged_at = 0.0

    def bad(self, text: str) -> None:
        """Report the failure. Logged only when it is new or has changed."""
        now = self.clock()
        if not self.failing:
            self.failing, self.since, self.count = True, now, 0
        self.count += 1
        if text != self.text:
            self.text, self.logged_at = text, now
            self.logger.warning("%s: %s", self.name, text)
        elif now - self.logged_at >= self.remind_s:
            self.logged_at = now
            self.logger.warning(
                "%s: still %s (%d times in %s)",
                self.name,
                text,
                self.count,
                _duration(now - self.since),
            )

    def ok(self, text: str = "working again") -> None:
        """Report that it works. Logged only after a failure."""
        if self.failing:
            self.logger.info(
                "%s: %s after %s (%d failures)",
                self.name,
                text,
                _duration(self.clock() - self.since),
                self.count,
            )
        self.failing, self.text, self.count = False, "", 0

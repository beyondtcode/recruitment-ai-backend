"""One pace for every request the agent sends to LinkedIn.

A run sends LinkedIn well over a thousand requests (link checks, searches,
paging, watch-list companies, company pages). Sent back to back they get the
server's IP rate-limited (HTTP 429, empty search pages), so:

- every LinkedIn request waits for its turn: at least LINKEDIN_MIN_INTERVAL
  seconds (default 2) after the previous one, across threads and tasks;
- a 429 starts a cool-down (LINKEDIN_COOLDOWN seconds, default 90) during which
  every LinkedIn request waits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_next_at = 0.0
cooldowns = 0  # how many times this process hit a 429 (reported in the run summary)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def wait_turn() -> None:
    """Block until this request may go out (thread-safe)."""
    global _next_at
    with _lock:
        delay = _next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _next_at = time.monotonic() + _env_float("LINKEDIN_MIN_INTERVAL", 2.0)


async def await_turn() -> None:
    await asyncio.to_thread(wait_turn)


def cooldown(reason: str = "") -> None:
    """LinkedIn answered 429: hold every LinkedIn request for a while."""
    global _next_at, cooldowns
    seconds = _env_float("LINKEDIN_COOLDOWN", 90.0)
    with _lock:
        _next_at = max(_next_at, time.monotonic() + seconds)
        cooldowns += 1
    logger.warning("linkedin: rate limited%s, pausing LinkedIn requests for %.0fs", f" ({reason})" if reason else "", seconds)

"""Throttle-safe Slack reads for the daycare desk.

Slack throttles conversations.history / .replies for the Loma app SILENTLY: past a burst it
returns ok=true with zero messages and no Retry-After (reproduced 2026-10-27: 19, 19, 19, 0).
So an empty result is never trusted:

  history(...)  empty → retry 3 s, 10 s, 30 s; still empty → SuspectRead (caller decides)
  replies(...)  the parent must come back (a known thread always has one); otherwise retry,
                then SuspectRead. Callers that dedupe on replies fail closed on SuspectRead.

Every read is paced (MIN_GAP seconds apart, process-wide) and a real `ratelimited` error
honours Retry-After.
"""
from __future__ import annotations

import threading
import time

MIN_GAP = 1.5
BACKOFF = (3, 10, 30)
_gap_lock = threading.Lock()
_last = [0.0]


class SuspectRead(RuntimeError):
    """Slack returned nothing where something must exist: throttled, not truth."""


def _pace(sleep) -> None:
    with _gap_lock:
        wait = _last[0] + MIN_GAP - time.monotonic()
        if wait > 0:
            sleep(wait)
        _last[0] = time.monotonic()


def _retry_after(exc) -> float | None:
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        if resp.get("error") != "ratelimited":
            return None
        headers = getattr(resp, "headers", None) or {}
        return float(headers.get("Retry-After") or headers.get("retry-after") or 30)
    except Exception:  # noqa: BLE001
        return None


def _call(fn, kwargs, ok, sleep, backoff, what):
    sleep = sleep or time.sleep
    backoff = BACKOFF if backoff is None else backoff
    last_err = None
    for attempt in range(len(backoff) + 1):
        _pace(sleep)
        try:
            resp = fn(**kwargs)
            msgs = list((resp.get("messages") if hasattr(resp, "get") else None) or [])
            if ok(msgs):
                return msgs
            last_err = f"{what}: empty/partial result (attempt {attempt + 1})"
        except Exception as e:  # noqa: BLE001
            ra = _retry_after(e)
            if ra is None:
                raise
            last_err = f"{what}: ratelimited"
            sleep(min(ra, 60))
        if attempt < len(backoff):
            sleep(backoff[attempt])
    raise SuspectRead(last_err or f"{what}: suspect")


def history(web, channel: str, oldest: str = "", limit: int = 200, sleep=None, backoff=None) -> list[dict]:
    """Channel history; an empty page is retried and finally raises SuspectRead."""
    kw = {"channel": channel, "limit": limit}
    if oldest:
        kw["oldest"] = oldest
    return _call(web.conversations_history, kw, lambda m: bool(m), sleep, backoff, f"history {channel}")


def replies(web, channel: str, ts: str, limit: int = 100, sleep=None, backoff=None, **extra) -> list[dict]:
    """Thread replies including the parent; a result without the parent raises SuspectRead."""
    kw = {"channel": channel, "ts": ts, "limit": limit, **extra}
    has_parent = lambda m: any(x.get("ts") == ts for x in m)  # noqa: E731
    return _call(web.conversations_replies, kw, has_parent, sleep, backoff, f"replies {channel}/{ts}")

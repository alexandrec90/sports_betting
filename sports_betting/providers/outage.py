"""Ride out a provider's momentary outage inside the run that met it.

A gateway error or a dropped connection says nothing about the key, the plan or the
request, and it is usually gone in seconds. Failing the sport on the first one left it
uncollected, and its job `degraded`, for a whole six-hour interval: one 502 on one free
listing did both on 2026-11-25.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

#: Seconds waited before each further attempt.
OUTAGE_WAITS = (5.0, 20.0)
#: The statuses a gateway answers for an origin it cannot reach. A 500 is the provider's
#: own answer to this request and is not repeated.
OUTAGE_STATUSES = frozenset({502, 503, 504})

_sleep: Callable[[float], None] = time.sleep


def send_through_outage(
    send: Callable[[], httpx.Response], *, before_request: Callable[[], None]
) -> httpx.Response:
    """`send()`'s response, sent again after each of `OUTAGE_WAITS` while the provider is out.

    Every attempt passes `before_request`, so a repeat is paced and claims its budget: a
    gateway error does not say whether the provider counted the request behind it. The
    last attempt's response is returned, or its transport error raised, as it came. An
    outage longer than the waits therefore still fails the sport and still shows.
    """
    for wait in OUTAGE_WAITS:
        before_request()
        try:
            response = send()
        except httpx.TransportError:
            pass
        else:
            if response.status_code not in OUTAGE_STATUSES:
                return response
        _sleep(wait)
    before_request()
    return send()

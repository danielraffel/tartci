#!/usr/bin/env python3
"""Is a cached measurement too old to trust? One rule for every status line.

Status surfaces render state that a periodic job wrote: tartci's skew (the
launchd watchdog, every 30 min), tool freshness (the same pass), host vitals
(the sensor, every 60 s). When that job stops, the file keeps saying what was
true when it last ran. On m3 the skew line read "1 commits behind main" for
two days after the host was current, because the watchdog had not run a pass
in that time and nothing compared the measurement's age with how often it is
refreshed.

A measurement is STALE once it is older than STALE_FACTOR refresh intervals:
one missed run is ordinary, three in a row means the refresher is not running.
Python 3.9-safe: the launchd python on fleet hosts is /usr/bin/python3.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Optional, Union

STALE_FACTOR = 3
# The clock every age is measured against; tests replace it.
clock = time.time


def age_seconds(stamp: Union[str, int, float, None],
                now: Optional[float] = None) -> Optional[float]:
    """Seconds since an ISO-8601 UTC stamp (or epoch seconds); None if unreadable."""
    now = clock() if now is None else now
    if isinstance(stamp, bool) or stamp is None:
        return None
    if isinstance(stamp, (int, float)):
        return max(0.0, now - float(stamp))
    try:
        then = dt.datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None
    return max(0.0, now - then)


def _span(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 2 * 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def stale_note(stamp: Union[str, int, float, None], interval_s: float, refresher: str,
               now: Optional[float] = None) -> Optional[str]:
    """None while fresh; otherwise one sentence naming the age and the refresher.

    An unreadable stamp is stale too: a measurement whose time cannot be read
    cannot be shown as current.
    """
    age = age_seconds(stamp, now)
    limit = STALE_FACTOR * float(interval_s)
    if age is not None and age <= limit:
        return None
    measured = "at an unreadable time" if age is None else f"{_span(age)} ago"
    return (f"STALE (measured {measured}, older than {STALE_FACTOR} x {_span(interval_s)}; "
            f"is the {refresher} running?)")

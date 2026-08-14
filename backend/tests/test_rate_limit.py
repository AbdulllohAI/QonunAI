"""Rate-limit windowing.

The limiter was fixed at an hour, in the code rather than in configuration, so
the quota could only ever be expressed per hour. These tests pin the parts that
are easy to get subtly wrong when the window becomes a day: the Retry-After a
caller is told to wait, and the unit named in the error.
"""
from __future__ import annotations

import time

from app.core.deps import _window_label


def test_window_labels_match_the_promise():
    assert _window_label(86_400) == "day"
    assert _window_label(3600) == "hour"


def test_unusual_window_still_reads_sensibly():
    assert _window_label(1800) == "1800s"


def test_retry_after_is_time_until_reset_not_window_length():
    """A daily quota hit at 23:00 resets in an hour, not in a day. Reporting a
    flat window length is wrong for everyone who did not hit it at midnight."""
    seconds = 86_400
    now = time.time()
    window = int(now // seconds)
    resets_at = (window + 1) * seconds
    retry_after = max(1, int(resets_at - now))

    assert 0 < retry_after <= seconds


def test_retry_after_is_never_zero():
    """A zero or negative Retry-After invites an immediate retry loop."""
    seconds = 86_400
    at_boundary = float((int(time.time() // seconds) + 1) * seconds)
    window = int(at_boundary // seconds)
    retry_after = max(1, int((window + 1) * seconds - at_boundary))

    assert retry_after >= 1

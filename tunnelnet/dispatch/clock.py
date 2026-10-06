"""Wall clock seam.

Everything in dispatch takes a ``Callable[[], float]`` returning epoch seconds
so tests can drive time deterministically; a UTC stamp helper is provided for
human readable audit output.
"""

from __future__ import annotations

import time
from collections.abc import Callable


def utc_stamp(epoch: float) -> str:
    """Compact UTC timestamp for audit and reports."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def system_clock() -> Callable[[], float]:
    return time.time

"""Chinese disclosure calendar dates; observation instants remain unchanged.

Use this only when comparing a Chinese publication/founding/as-of date with
an instant. Retry clocks and exchange trading dates have separate semantics.
"""
from __future__ import annotations

import datetime as dt
from datetime import datetime as Datetime
import re
from zoneinfo import ZoneInfo

CHINA = ZoneInfo('Asia/Shanghai')
_STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z')


def china_date(value):
    """Return the China calendar date of a timezone-aware instant, or None."""
    try:
        if isinstance(value, str):
            if not _STAMP.fullmatch(value):
                return None
            value = Datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not isinstance(value, Datetime) or value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(CHINA).date()
    except (ValueError, TypeError, OverflowError):
        return None


def china_today(now=None):
    value = china_date(now if now is not None else dt.datetime.now(dt.timezone.utc))
    if value is None:
        raise ValueError('INVALID_OBSERVATION_TIME')
    return value.isoformat()

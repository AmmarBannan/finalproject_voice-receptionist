"""Dates and times are code, not vibes: the LLM extracts a phrase, this module computes the date."""
from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def resolve_date(ref: str | None, today: date) -> date | None:
    """Turn the intake agent's date_ref into a date, relative to the CLINIC's today.

    Accepted: YYYY-MM-DD, today, tomorrow, day_after_tomorrow, <weekday>, next_week_<weekday>.
    A bare weekday means its first occurrence AFTER today ("Tuesday" on a Monday is tomorrow).
    """
    if not ref:
        return None
    r = ref.strip().lower().replace(" ", "_")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", r):
        try:
            return date.fromisoformat(r)
        except ValueError:
            return None
    if r == "today":
        return today
    if r == "tomorrow":
        return today + timedelta(days=1)
    if r in ("day_after_tomorrow", "after_tomorrow"):
        return today + timedelta(days=2)
    extra = 0
    for prefix in ("next_week_", "following_"):
        if r.startswith(prefix):
            r, extra = r[len(prefix):], 7
    r = r.removeprefix("this_").removeprefix("next_")
    if r in WEEKDAYS:
        delta = (WEEKDAYS.index(r) - today.weekday()) % 7 or 7
        return today + timedelta(days=delta + extra)
    return None


def to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def norm_hhmm(value: str | None) -> str | None:
    """'4:30' / '16:30' / '1630' -> '16:30' (24 h). Returns None for anything else."""
    if not value:
        return None
    m = re.fullmatch(r"\s*(\d{1,2})(?::?(\d{2}))?\s*", str(value))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def spoken_time(hhmm: str) -> str:
    """'16:30' -> '4:30 PM' (how a receptionist says it)."""
    h, m = map(int, hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def spoken_day(d: date, today: date, on: bool = False) -> str:
    """'tomorrow (Tuesday, November 3)' / 'on Thursday, November 5' (with on=True)."""
    label = f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"
    if d == today:
        return f"today ({label})"
    if d == today + timedelta(days=1):
        return f"tomorrow ({label})"
    return f"on {label}" if on else label


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])|\b(\d{1,2}):(\d{2})\b", re.I)


def time_mentions(text: str) -> list[set[int]]:
    """Every clock time mentioned in a reply, as the set of minutes-since-midnight it may mean
    ("4:30" without AM/PM may be 04:30 or 16:30). Used by the output check."""
    out: list[set[int]] = []
    for m in _TIME_RE.finditer(text):
        if m.group(1):
            h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
            if h > 12:
                continue
            out.append({(h % 12 + (12 if ap.startswith("p") else 0)) * 60 + mi})
        else:
            h, mi = int(m.group(4)), int(m.group(5))
            if h <= 23 and mi <= 59:
                out.append({h * 60 + mi, (h % 12 + 12) * 60 + mi} if h <= 12 else {h * 60 + mi})
    return out

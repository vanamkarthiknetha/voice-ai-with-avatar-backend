"""
Hardcoded clinic availability.

`fetch_slots` returns these minus any (date, time) that already has a
'booked' appointment, so the agent never offers a taken slot.
"""

from datetime import date, timedelta

# Times the clinic offers every working day.
DAILY_TIMES = ["09:00 AM", "10:00 AM", "11:00 AM", "02:00 PM", "03:00 PM", "04:00 PM"]


def _next_working_days(n: int = 3) -> list[str]:
    """Return the next `n` weekdays as ISO date strings (skip Sat/Sun)."""
    days: list[str] = []
    cursor = date.today() + timedelta(days=1)  # start tomorrow
    while len(days) < n:
        if cursor.weekday() < 5:  # Mon=0 .. Fri=4
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def all_slots() -> list[dict]:
    """Full catalogue of offered slots over the next few working days."""
    slots: list[dict] = []
    for d in _next_working_days(3):
        for t in DAILY_TIMES:
            slots.append({"date": d, "time": t})
    return slots

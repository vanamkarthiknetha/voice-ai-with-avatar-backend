"""
Data-access layer for users, appointments and call summaries.

All functions are plain (synchronous) sqlite calls. They are cheap, but the
agent runs inside an asyncio loop, so the agent wraps these in
`asyncio.to_thread(...)` to avoid blocking the event loop. FastAPI calls them
directly from threadpool (sync) endpoints.

`book_appointment` raises `SlotTakenError` when the requested slot already has
an active booking — this is how double-booking is prevented.
"""

import sqlite3

from app import db
from app.slots import all_slots


class SlotTakenError(Exception):
    """Raised when a (date, time) slot is already booked by someone."""


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def upsert_user(phone: str, name: str | None = None) -> dict:
    """Identify (or create) a user by phone number — the unique ID."""
    conn = db.connect()
    try:
        existing = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        if existing is None:
            conn.execute("INSERT INTO users (phone, name) VALUES (?, ?)", (phone, name))
        elif name and existing["name"] != name:
            conn.execute("UPDATE users SET name = ? WHERE phone = ?", (name, phone))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        return dict(row)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Slots
# --------------------------------------------------------------------------- #
def available_slots() -> list[dict]:
    """Catalogue slots minus those already booked."""
    conn = db.connect()
    try:
        booked = {
            (r["date"], r["time"])
            for r in conn.execute(
                "SELECT date, time FROM appointments WHERE status = 'booked'"
            ).fetchall()
        }
    finally:
        conn.close()
    return [s for s in all_slots() if (s["date"], s["time"]) not in booked]


# --------------------------------------------------------------------------- #
# Appointments
# --------------------------------------------------------------------------- #
def _row_to_appt(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "phone": row["phone"],
        "name": row["name"],
        "date": row["date"],
        "time": row["time"],
        "reason": row["reason"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def book_appointment(
    phone: str, name: str | None, date: str, time: str, reason: str | None = None
) -> dict:
    """Create a booking. Raises SlotTakenError if the slot is taken."""
    conn = db.connect()
    try:
        taken = conn.execute(
            "SELECT 1 FROM appointments WHERE date = ? AND time = ? AND status = 'booked'",
            (date, time),
        ).fetchone()
        if taken:
            raise SlotTakenError(f"{date} at {time} is already booked")

        cur = conn.execute(
            "INSERT INTO appointments (phone, name, date, time, reason) VALUES (?, ?, ?, ?, ?)",
            (phone, name, date, time, reason),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_appt(row)
    except sqlite3.IntegrityError as exc:
        # Hit the unique index race — treat as slot taken.
        raise SlotTakenError(f"{date} at {time} is already booked") from exc
    finally:
        conn.close()


def list_appointments(phone: str, include_cancelled: bool = False) -> list[dict]:
    """Return a user's bookings, most recent first."""
    conn = db.connect()
    try:
        if include_cancelled:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE phone = ? ORDER BY date, time", (phone,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE phone = ? AND status = 'booked' ORDER BY date, time",
                (phone,),
            ).fetchall()
        return [_row_to_appt(r) for r in rows]
    finally:
        conn.close()


def cancel_appointment(phone: str, date: str, time: str) -> dict | None:
    """Cancel a user's booking at the given date/time. Returns it, or None."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM appointments WHERE phone = ? AND date = ? AND time = ? AND status = 'booked'",
            (phone, date, time),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE appointments SET status = 'cancelled', updated_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
        return _row_to_appt(row)
    finally:
        conn.close()


def modify_appointment(
    phone: str, old_date: str, old_time: str, new_date: str, new_time: str
) -> dict:
    """
    Move a booking to a new slot. Raises SlotTakenError if the new slot is
    taken, KeyError if the original booking is not found.
    """
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM appointments WHERE phone = ? AND date = ? AND time = ? AND status = 'booked'",
            (phone, old_date, old_time),
        ).fetchone()
        if row is None:
            raise KeyError("original appointment not found")

        clash = conn.execute(
            "SELECT 1 FROM appointments WHERE date = ? AND time = ? AND status = 'booked' AND id != ?",
            (new_date, new_time, row["id"]),
        ).fetchone()
        if clash:
            raise SlotTakenError(f"{new_date} at {new_time} is already booked")

        conn.execute(
            "UPDATE appointments SET date = ?, time = ?, updated_at = datetime('now') WHERE id = ?",
            (new_date, new_time, row["id"]),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM appointments WHERE id = ?", (row["id"],)).fetchone()
        return _row_to_appt(updated)
    except sqlite3.IntegrityError as exc:
        raise SlotTakenError(f"{new_date} at {new_time} is already booked") from exc
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Call summaries
# --------------------------------------------------------------------------- #
def save_summary(room: str, data: dict) -> None:
    """Persist the end-of-call summary keyed by room name."""
    import json

    conn = db.connect()
    try:
        conn.execute(
            """
            INSERT INTO call_summaries
                (room, phone, name, summary, intent, preferences,
                 extracted_date, extracted_time, appointments_json)
            VALUES
                (:room, :phone, :name, :summary, :intent, :preferences,
                 :extracted_date, :extracted_time, :appointments_json)
            ON CONFLICT(room) DO UPDATE SET
                phone=excluded.phone, name=excluded.name, summary=excluded.summary,
                intent=excluded.intent, preferences=excluded.preferences,
                extracted_date=excluded.extracted_date, extracted_time=excluded.extracted_time,
                appointments_json=excluded.appointments_json
            """,
            {
                "room": room,
                "phone": data.get("phone"),
                "name": data.get("name"),
                "summary": data.get("summary"),
                "intent": data.get("intent"),
                "preferences": data.get("preferences"),
                "extracted_date": data.get("date"),
                "extracted_time": data.get("time"),
                "appointments_json": json.dumps(data.get("appointments", [])),
            },
        )
        conn.commit()
    finally:
        conn.close()


def get_summary(room: str) -> dict | None:
    import json

    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM call_summaries WHERE room = ?", (room,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["appointments"] = json.loads(out.pop("appointments_json") or "[]")
        # Surface extracted date/time under the keys the UI expects.
        out["date"] = out.pop("extracted_date", None)
        out["time"] = out.pop("extracted_time", None)
        return out
    finally:
        conn.close()

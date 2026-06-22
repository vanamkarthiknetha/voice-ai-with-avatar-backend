"""
End-of-call summary generation.

Takes the conversation transcript plus the structured fields the agent
collected, and asks Gemini to produce a concise JSON summary. Designed to run
in well under the 10-second budget from the brief. If the LLM call fails for any
reason, we fall back to a deterministic summary built from the collected fields,
so the UI always gets something.
"""

import json
import logging
import os
from datetime import datetime, timezone

from livekit.agents import llm as lk_llm

logger = logging.getLogger("frontdesk.summary")

_SUMMARY_PROMPT = """You are summarizing a healthcare front-desk phone call.
Extract structured details and return ONLY a JSON object (no markdown fences)
with these keys:
- "summary": 2-3 sentence plain-language recap of what happened on the call.
- "name": the patient's name as stated, or "" if not given.
- "phone": the patient's phone number as stated, or "" if not given.
- "date": the appointment date the patient asked about (ISO YYYY-MM-DD if you can
          resolve it, else the phrase they used), or "" if none.
- "time": the appointment time the patient asked about (e.g. "10:00 AM"), or "" if none.
- "intent": the patient's main intent (e.g. "book appointment", "reschedule", "cancel", "inquiry").
- "preferences": any other stated preferences (doctor, urgency, etc.) or "" if none.

Conversation transcript:
{transcript}

Known facts (use these to fill name/phone/date/time if the transcript is unclear):
- Patient name: {name}
- Patient phone: {phone}
- Appointments on file after this call: {appointments}
"""


def _transcript_from_history(history: lk_llm.ChatContext) -> str:
    lines: list[str] = []
    try:
        copy = history.copy(
            exclude_empty_message=True,
            exclude_instructions=True,
            exclude_function_call=True,
        )
        for msg in copy.items:
            text = (msg.text_content or "").strip()
            if not text:
                continue
            who = "Patient" if msg.role == "user" else "Mira"
            lines.append(f"{who}: {text}")
    except Exception:
        logger.exception("could not read chat history")
    return "\n".join(lines) if lines else "(no transcript captured)"


async def _gemini_json(prompt: str) -> dict:
    """Single-shot Gemini call that returns parsed JSON."""
    from google import genai  # imported lazily so the worker starts without it

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    model = os.getenv("SUMMARY_MODEL", "gemini-2.5-flash")

    resp = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    return json.loads(resp.text)


async def generate_summary(
    *,
    room: str,
    history: lk_llm.ChatContext,
    name: str | None,
    phone: str | None,
    appointments: list[dict],
) -> dict:
    """Build the structured call summary. Always returns a dict."""
    transcript = _transcript_from_history(history)
    appts_str = (
        ", ".join(f"{a['date']} {a['time']} ({a.get('reason') or 'visit'})" for a in appointments)
        or "none"
    )

    llm_part: dict = {}
    try:
        prompt = _SUMMARY_PROMPT.format(
            transcript=transcript, name=name or "unknown", phone=phone or "unknown", appointments=appts_str
        )
        llm_part = await _gemini_json(prompt)
    except Exception:
        logger.exception("LLM summary failed — using fallback")
        llm_part = {
            "summary": f"Call with {name or 'a patient'}. {len(appointments)} active appointment(s) on file.",
            "intent": "appointment management",
            "preferences": "",
        }

    # Prefer authoritative values from tool calls / bookings, then LLM extraction.
    first_appt = appointments[0] if appointments else {}
    return {
        "room": room,
        "name": name or llm_part.get("name") or "",
        "phone": phone or llm_part.get("phone") or "",
        "date": first_appt.get("date") or llm_part.get("date") or "",
        "time": first_appt.get("time") or llm_part.get("time") or "",
        "summary": llm_part.get("summary", ""),
        "intent": llm_part.get("intent", ""),
        "preferences": llm_part.get("preferences", ""),
        "appointments": appointments,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

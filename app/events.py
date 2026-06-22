"""
Push UI events from the agent to the frontend over a LiveKit data channel.

The frontend subscribes to two data topics:
  - "tool_events"  -> per-tool-call status chips ("Fetching slots…", "Booked ✅")
  - "call_summary" -> the final end-of-call summary card

Both are sent reliably via `room.local_participant.publish_data(...)`.
Satisfies the PDF's "Whenever a tool is called: show it visually on screen"
and "Call Summary ... Show on UI" requirements.
"""

import json
import logging

from livekit import rtc

logger = logging.getLogger("frontdesk.events")

TOPIC_TOOL = "tool_events"
TOPIC_SUMMARY = "call_summary"


async def publish_tool_event(
    room: rtc.Room,
    *,
    tool: str,
    status: str,  # "running" | "done" | "error"
    label: str,
    data: dict | None = None,
) -> None:
    """Send a single tool-call status update to the UI."""
    payload = {"type": "tool_call", "tool": tool, "status": status, "label": label}
    if data:
        payload["data"] = data
    try:
        await room.local_participant.publish_data(
            json.dumps(payload).encode("utf-8"),
            reliable=True,
            topic=TOPIC_TOOL,
        )
    except Exception:
        logger.exception("failed to publish tool event %s/%s", tool, status)


async def publish_summary(room: rtc.Room, summary: dict) -> None:
    """Send the end-of-call summary card to the UI."""
    payload = {"type": "summary", **summary}
    try:
        await room.local_participant.publish_data(
            json.dumps(payload).encode("utf-8"),
            reliable=True,
            topic=TOPIC_SUMMARY,
        )
    except Exception:
        logger.exception("failed to publish summary")

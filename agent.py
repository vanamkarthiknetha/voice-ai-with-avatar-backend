"""
Mykare Voice AI — healthcare front-desk agent (LiveKit Agents worker).

A patient-facing receptionist ("Mira") that books and manages appointments
entirely by voice, with:

  - STT  : Deepgram nova-3
  - LLM  : Google Gemini 2.5 Flash (reliable function calling)
  - TTS  : Deepgram aura-2
  - Avatar (optional): Beyond Presence — enabled automatically if BEY_API_KEY is set
  - Tools: identify_user, fetch_slots, book_appointment, retrieve_appointments,
           cancel_appointment, modify_appointment, end_conversation
  - DB   : SQLite (shared with the FastAPI server)
  - UI   : every tool call is pushed to the frontend as a status chip, and a
           structured summary is generated + pushed when the call ends.

Run:  python agent.py dev
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    RoomInputOptions,
    RoomOutputOptions,
    RunContext,
    WorkerOptions,
    cli,
    llm,
    stt,
    tts,
)
from livekit.agents.llm import function_tool
from livekit.plugins import deepgram, google, noise_cancellation, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

try:
    # Import at module load (main thread). LiveKit requires plugins to be
    # registered on the main thread — a lazy import inside the job task fails.
    from livekit.plugins import bey
except ImportError:
    bey = None

from app import db, events, repository
from app.prompts import AGENT_INSTRUCTIONS
from app.summary import generate_summary

logger = logging.getLogger("frontdesk")
logging.basicConfig(level=logging.INFO)

load_dotenv()


# --------------------------------------------------------------------------- #
# Shared call state (LiveKit "userdata")
# --------------------------------------------------------------------------- #
@dataclass
class CallState:
    """Per-call state threaded through every tool via RunContext.userdata."""

    ctx: JobContext
    phone: str | None = None
    name: str | None = None
    last_intent: str | None = None
    ended: bool = False
    booked_in_call: list[dict] = field(default_factory=list)

    @property
    def room(self):
        return self.ctx.room


# --------------------------------------------------------------------------- #
# The agent + its tools
# --------------------------------------------------------------------------- #
class FrontDeskAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=AGENT_INSTRUCTIONS)

    async def on_enter(self):
        # Greet the patient as soon as they connect.
        self.session.generate_reply()

    # --- helpers ---------------------------------------------------------- #
    async def _emit(self, ctx: RunContext, tool: str, status: str, label: str, data: dict | None = None):
        await events.publish_tool_event(
            ctx.userdata.room, tool=tool, status=status, label=label, data=data
        )

    # --- tools ------------------------------------------------------------ #
    @function_tool
    async def identify_user(self, ctx: RunContext, phone: str, name: str | None = None):
        """Identify the patient by phone number (their unique ID). Call this once
        you have a confirmed phone number; pass the name too if you already know it.

        Args:
            phone: The patient's phone number, digits only or E.164 (e.g. +919032363511).
            name: The patient's full name, if known.
        """
        state: CallState = ctx.userdata
        await self._emit(ctx, "identify_user", "running", "Identifying patient…")

        user = await asyncio.to_thread(repository.upsert_user, phone, name)
        state.phone = phone
        if name:
            state.name = name
        elif user.get("name"):
            state.name = user["name"]

        existing = await asyncio.to_thread(repository.list_appointments, phone)
        await self._emit(
            ctx, "identify_user", "done",
            f"Patient identified ({phone})",
            {"phone": phone, "name": state.name, "existing_appointments": len(existing)},
        )
        return {
            "phone": phone,
            "name": state.name,
            "returning_patient": bool(existing),
            "existing_appointments": existing,
        }

    @function_tool
    async def fetch_slots(self, ctx: RunContext):
        """Fetch real, currently-available appointment slots. Always call this
        before offering times — never invent slots."""
        await self._emit(ctx, "fetch_slots", "running", "Fetching available slots…")
        slots = await asyncio.to_thread(repository.available_slots)
        await self._emit(
            ctx, "fetch_slots", "done", f"{len(slots)} slots available", {"slots": slots[:12]}
        )
        # Hand the model a compact list to read out.
        return {"available_slots": slots[:12]}

    @function_tool
    async def book_appointment(
        self, ctx: RunContext, date: str, time: str, reason: str | None = None,
        name: str | None = None, phone: str | None = None,
    ):
        """Book an appointment. Requires the patient's phone (from identify_user).
        Prevents double-booking and confirms the exact date and time.

        Args:
            date: ISO date, e.g. 2026-06-23.
            time: Slot time exactly as offered, e.g. "10:00 AM".
            reason: Reason for the visit / intent.
            name: Patient name (optional if already identified).
            phone: Patient phone (optional if already identified).
        """
        state: CallState = ctx.userdata
        state.phone = phone or state.phone
        state.name = name or state.name
        if reason:
            state.last_intent = reason

        if not state.phone:
            return {"error": "no_phone", "message": "Ask for the patient's phone number and call identify_user first."}

        await self._emit(ctx, "book_appointment", "running", f"Booking {date} at {time}…")
        try:
            appt = await asyncio.to_thread(
                repository.book_appointment, state.phone, state.name, date, time, reason
            )
        except repository.SlotTakenError:
            await self._emit(
                ctx, "book_appointment", "error", f"{time} on {date} is taken",
                {"date": date, "time": time},
            )
            return {"error": "slot_taken", "message": f"{date} at {time} is already booked. Offer another slot."}

        state.booked_in_call.append(appt)
        await self._emit(
            ctx, "book_appointment", "done", f"Booked ✅ {date} at {time}", {"appointment": appt}
        )
        return {"booked": True, "appointment": appt}

    @function_tool
    async def retrieve_appointments(self, ctx: RunContext, phone: str | None = None):
        """List the patient's current (booked) appointments so you can read them back."""
        state: CallState = ctx.userdata
        state.phone = phone or state.phone
        if not state.phone:
            return {"error": "no_phone", "message": "Ask for the patient's phone number first."}

        await self._emit(ctx, "retrieve_appointments", "running", "Looking up your appointments…")
        appts = await asyncio.to_thread(repository.list_appointments, state.phone)
        await self._emit(
            ctx, "retrieve_appointments", "done", f"{len(appts)} appointment(s) found",
            {"appointments": appts},
        )
        return {"appointments": appts}

    @function_tool
    async def cancel_appointment(self, ctx: RunContext, date: str, time: str, phone: str | None = None):
        """Cancel the patient's appointment at a specific date and time.

        Args:
            date: ISO date of the appointment to cancel.
            time: Time of the appointment to cancel, e.g. "10:00 AM".
        """
        state: CallState = ctx.userdata
        state.phone = phone or state.phone
        if not state.phone:
            return {"error": "no_phone", "message": "Identify the patient first."}

        await self._emit(ctx, "cancel_appointment", "running", f"Cancelling {date} at {time}…")
        cancelled = await asyncio.to_thread(repository.cancel_appointment, state.phone, date, time)
        if cancelled is None:
            await self._emit(ctx, "cancel_appointment", "error", "No matching appointment")
            return {"error": "not_found", "message": "No booked appointment found at that date and time."}

        await self._emit(ctx, "cancel_appointment", "done", f"Cancelled {date} at {time}", {"appointment": cancelled})
        return {"cancelled": True, "appointment": cancelled}

    @function_tool
    async def modify_appointment(
        self, ctx: RunContext, old_date: str, old_time: str, new_date: str, new_time: str,
        phone: str | None = None,
    ):
        """Reschedule an existing appointment to a new slot.

        Args:
            old_date: ISO date of the existing appointment.
            old_time: Time of the existing appointment.
            new_date: ISO date of the new slot.
            new_time: Time of the new slot.
        """
        state: CallState = ctx.userdata
        state.phone = phone or state.phone
        if not state.phone:
            return {"error": "no_phone", "message": "Identify the patient first."}

        await self._emit(
            ctx, "modify_appointment", "running",
            f"Rescheduling to {new_date} at {new_time}…",
        )
        try:
            appt = await asyncio.to_thread(
                repository.modify_appointment, state.phone, old_date, old_time, new_date, new_time
            )
        except KeyError:
            await self._emit(ctx, "modify_appointment", "error", "Original appointment not found")
            return {"error": "not_found", "message": "Could not find the original appointment to move."}
        except repository.SlotTakenError:
            await self._emit(ctx, "modify_appointment", "error", f"{new_time} on {new_date} is taken")
            return {"error": "slot_taken", "message": f"{new_date} at {new_time} is taken. Offer another slot."}

        await self._emit(
            ctx, "modify_appointment", "done", f"Moved to {new_date} at {new_time}", {"appointment": appt}
        )
        return {"modified": True, "appointment": appt}

    @function_tool
    async def end_conversation(self, ctx: RunContext):
        """End the call. Call this after a brief warm goodbye when the patient is
        finished. This generates and shows the call summary, then hangs up."""
        state: CallState = ctx.userdata
        if state.ended:
            return None
        state.ended = True

        await self._emit(ctx, "end_conversation", "running", "Wrapping up & summarizing…")
        await self.session.say("Thank you for calling Mykare Health. Take care and goodbye!")

        await _finalize_call(self.session, state)
        state.ctx.shutdown("conversation ended by patient")  # sync, not awaitable
        return None


# --------------------------------------------------------------------------- #
# Summary finalization (runs on end_conversation AND on disconnect)
# --------------------------------------------------------------------------- #
async def _finalize_call(session: AgentSession, state: CallState) -> None:
    """Generate, persist and broadcast the end-of-call summary (idempotent)."""
    if getattr(state, "_summarized", False):
        return
    state._summarized = True  # type: ignore[attr-defined]

    try:
        appointments = (
            await asyncio.to_thread(repository.list_appointments, state.phone)
            if state.phone else []
        )
        summary = await generate_summary(
            room=state.room.name,
            history=session.history,
            name=state.name,
            phone=state.phone,
            appointments=appointments,
        )
        await asyncio.to_thread(repository.save_summary, state.room.name, summary)
        await events.publish_summary(state.room, summary)
        await events.publish_tool_event(
            state.room, tool="end_conversation", status="done", label="Summary ready 📝"
        )
        logger.info("call summary generated for room %s", state.room.name)
    except Exception:
        logger.exception("failed to finalize call summary")


# --------------------------------------------------------------------------- #
# Worker entrypoint
# --------------------------------------------------------------------------- #
def prewarm(proc: JobProcess):
    """Load the VAD model once per worker process (shared across calls)."""
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    db.init_db()

    state = CallState(ctx=ctx)

    session = AgentSession[CallState](
        userdata=state,
        vad=ctx.proc.userdata["vad"],
        llm=_create_llm(),
        stt=_create_stt(),
        tts=_create_tts(),
        turn_detection=MultilingualModel(),
    )

    # Optional photorealistic avatar (Beyond Presence). Voice-only if no key.
    avatar_started = await _maybe_start_avatar(session, ctx)

    # If the patient leaves before saying goodbye, still produce a summary.
    def _on_participant_disconnected(participant):
        if not state.ended:
            asyncio.create_task(_finalize_call(session, state))

    ctx.room.on("participant_disconnected", _on_participant_disconnected)

    await session.start(
        agent=FrontDeskAgent(),
        room=ctx.room,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),
        ),
        # When an avatar publishes the audio track, the agent must not also
        # publish its own audio to the room.
        room_output_options=RoomOutputOptions(audio_enabled=not avatar_started),
    )


async def _maybe_start_avatar(session: AgentSession, ctx: JobContext) -> bool:
    """Start a Beyond Presence avatar if configured. Returns True if started."""
    avatar_id = os.getenv("BEY_AVATAR_ID")
    if not os.getenv("BEY_API_KEY") or not avatar_id:
        logger.info("avatar disabled (set BEY_API_KEY and BEY_AVATAR_ID to enable)")
        return False
    if bey is None:
        logger.warning("avatar requested but livekit-plugins-bey is not installed")
        return False
    try:
        avatar = bey.AvatarSession(avatar_id=avatar_id)
        await avatar.start(session, room=ctx.room)
        logger.info("Beyond Presence avatar started (%s)", avatar_id)
        return True
    except Exception:
        logger.exception("avatar failed to start — continuing voice-only")
        return False


def _create_llm() -> llm.LLM:
    # Disable Gemini "thinking" for voice: it adds latency and, on 2.5-flash,
    # can return an empty turn (0 output tokens) that stalls the conversation.
    from google.genai import types as genai_types

    return google.LLM(
        model=os.getenv("LLM_MODEL", "gemini-2.5-flash"),
        temperature=0.6,
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
    )


def _create_stt() -> stt.STT:
    return deepgram.STT(model="nova-3", language="multi")


def _create_tts() -> tts.TTS:
    return deepgram.TTS(model=os.getenv("TTS_MODEL", "aura-2-thalia-en"))


if __name__ == "__main__":
    # Standalone: run only the agent worker (no FastAPI). Use main.py to run both.
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="mykare-frontdesk",
            initialize_process_timeout=60,
        )
    )

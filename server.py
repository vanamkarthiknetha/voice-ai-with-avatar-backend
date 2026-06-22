"""
FastAPI backend for the Mykare Voice AI front-desk agent.

Responsibilities (kept separate from the agent worker in agent.py):
  - Mint LiveKit access tokens so the web frontend can join a room.
  - Expose read APIs the UI uses for the appointment panel & summary card.

The LiveKit agent worker auto-dispatches into whatever room the frontend joins,
so this server only needs to hand out a token — it does not start the agent.

Run:  uvicorn server:app --reload --port 8000
"""

import os
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from livekit import api

from app import db, repository
from app.slots import all_slots

load_dotenv()

# Must match the agent worker's WorkerOptions(agent_name=...) so LiveKit
# dispatches our front-desk agent (explicit dispatch) into the room.
AGENT_NAME = "mykare-frontdesk"

app = FastAPI(title="Mykare Voice AI — Front Desk API", version="1.0.0")

# Open CORS for the demo frontend. Restrict `allow_origins` in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class TokenRequest(BaseModel):
    identity: str | None = None
    name: str | None = None
    room: str | None = None


class TokenResponse(BaseModel):
    token: str
    url: str
    room: str
    identity: str


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/token", response_model=TokenResponse)
def create_token(req: TokenRequest) -> TokenResponse:
    """Issue a LiveKit join token for the web client. The agent worker joins the
    same room automatically."""
    lk_key = os.getenv("LIVEKIT_API_KEY")
    lk_secret = os.getenv("LIVEKIT_API_SECRET")
    lk_url = os.getenv("LIVEKIT_URL")
    if not (lk_key and lk_secret and lk_url):
        raise HTTPException(500, "LiveKit credentials are not configured on the server.")

    room = req.room or f"frontdesk-{uuid.uuid4().hex[:10]}"
    identity = req.identity or f"patient-{uuid.uuid4().hex[:6]}"

    token = (
        api.AccessToken(lk_key, lk_secret)
        .with_identity(identity)
        .with_name(req.name or identity)
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
        # Explicitly dispatch the front-desk agent into this room.
        .with_room_config(
            api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME, metadata=identity)]
            )
        )
        .to_jwt()
    )
    return TokenResponse(token=token, url=lk_url, room=room, identity=identity)


@app.get("/api/slots")
def get_slots() -> dict:
    """All offered slots plus the subset still available (UI convenience)."""
    return {"all": all_slots(), "available": repository.available_slots()}


@app.get("/api/appointments")
def get_appointments(phone: str, include_cancelled: bool = False) -> dict:
    """A patient's appointments, looked up by phone (their unique ID)."""
    if not phone:
        raise HTTPException(400, "phone query param is required")
    return {"phone": phone, "appointments": repository.list_appointments(phone, include_cancelled)}


@app.get("/api/summary/{room}")
def get_summary(room: str) -> dict:
    """The end-of-call summary for a given room."""
    summary = repository.get_summary(room)
    if summary is None:
        raise HTTPException(404, "No summary found for this room yet.")
    return summary

# Mykare Voice AI — Front Desk Agent (Backend)

A real-world **AI voice agent that acts as a healthcare front desk**. Patients
talk to it in natural speech; it understands intent, books and manages
appointments via tool calls, shows each action on screen, and produces a call
summary at the end.

This repo is the **backend**: a LiveKit Agents worker (the voice brain) plus a
small FastAPI service (tokens + read APIs). The web UI lives in the separate
frontend repo.

> Built with the [LiveKit Agents](https://docs.livekit.io/agents/) framework.
> The Mykare front-desk agent lives in `agent.py`.

---

## 🧠 What it does

| Capability | How |
|---|---|
| 🎤 Listen | Deepgram `nova-3` STT |
| 🧠 Understand | Google Gemini 2.5 Flash (function calling) |
| 🗣 Speak | Deepgram `aura-2` TTS |
| 👤 Avatar | Beyond Presence (optional, lip-synced) |
| 📅 Appointments | SQLite + tool calls (book / retrieve / cancel / modify) |
| 🖥 Tool UI | Each tool call is pushed to the frontend as a status chip |
| 📝 Summary | Gemini-generated summary pushed + stored at end of call |

### Tools the agent calls
`identify_user` · `fetch_slots` · `book_appointment` · `retrieve_appointments`
· `cancel_appointment` · `modify_appointment` · `end_conversation`

Each call publishes a `tool_events` data message to the room
(e.g. *“Fetching slots…”*, *“Booked ✅ 2026-06-23 at 10:00 AM”*) so the UI can
show it live. Double-booking is prevented at the database level.

---

## 🏗 Architecture

```
                       LiveKit Cloud (WebRTC room)
                        ▲                       ▲
        join token      │                       │  audio + data + avatar video
   ┌────────────────────┴───────┐     ┌─────────┴─────────────────────────┐
   │  FastAPI  (server.py)      │     │  Agent worker (agent.py)          │
   │  • POST /api/token         │     │  • FrontDeskAgent "Mira"          │
   │  • GET  /api/slots         │     │  • 7 function tools               │
   │  • GET  /api/appointments  │     │  • optional Beyond Presence avatar│
   │  • GET  /api/summary/{room}│     │  • end-of-call summary            │
   └────────────┬───────────────┘     └───────────────┬───────────────────┘
                │     shared SQLite (clinic.db, WAL)   │
                └──────────────────────────────────────┘
```

The frontend asks FastAPI for a token, joins the LiveKit room, and the agent
worker auto-dispatches into that same room. See [FLOW.md](FLOW.md) for the call
flow.

---

## 🚀 Setup

### 1. Virtual environment
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
```

### 2. Install
```bash
pip install -r requirements.txt
```

### 3. Configure
```bash
cp .env.example .env   # then fill in the values
```
Required: `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `LIVEKIT_URL`,
`DEEPGRAM_API_KEY`, `GOOGLE_API_KEY`, `HF_TOKEN`
([get an HF token](https://huggingface.co/settings/tokens/new?tokenType=read)).

Optional avatar: uncomment `livekit-plugins-bey` in `requirements.txt`, install
it, and set `BEY_API_KEY` + `BEY_AVATAR_ID`. Without these the agent runs
voice-only.

### 4. Download agent model files (one-time)
```bash
python agent.py download-files
```

### 5. Run (one process)
`main.py` starts the FastAPI server (Uvicorn, daemon thread on `:8000`) and the
LiveKit agent worker (main thread) together:
```bash
python main.py dev     # hot-reload for local dev
# python main.py start # production
```
`cli.run_app` reads `sys.argv`, so a LiveKit subcommand (`dev` / `start`) is
required. Point the frontend at `http://localhost:8000` for tokens and APIs.

> Prefer them apart? `python agent.py dev` runs the worker alone and
> `uvicorn server:app --port 8000` runs the API alone.

---

## 🔌 API reference

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/token` | Mint a LiveKit join token. Body: `{ "name"?, "room"?, "identity"? }` → `{ token, url, room, identity }` |
| `GET` | `/api/slots` | `{ all, available }` slot lists |
| `GET` | `/api/appointments?phone=...` | A patient's bookings |
| `GET` | `/api/summary/{room}` | End-of-call summary for a room |
| `GET` | `/healthz` | Health check |

---

## 🗄 Data model (SQLite)

- **users** — `phone` (PK, the unique patient ID), `name`
- **appointments** — `id, phone, name, date, time, reason, status` with a unique
  index on `(date, time)` for active bookings → **no double-booking**
- **call_summaries** — `room, phone, name, summary, intent, preferences,
  appointments_json, created_at`

---

## 💸 Cost per call (approx.)

A typical ~3-minute call:

| Component | Rate | ~Cost |
|---|---|---|
| Deepgram STT (nova-3) | ~$0.0043/min | ~$0.013 |
| Deepgram TTS (aura-2) | ~$0.030/1k chars | ~$0.02 |
| Gemini 2.5 Flash (LLM + summary) | ~$0.30/$2.50 per 1M tok | ~$0.01 |
| LiveKit Cloud | usage tier | ~$0.01 |
| Avatar (Beyond Presence, if on) | per-minute | varies |

**≈ $0.05 / call** voice-only (avatar adds the most when enabled).

---

## 📁 Layout
```
main.py             Single-process runner (FastAPI thread + agent worker)
agent.py            LiveKit agent worker (Mira, the front desk)
server.py           FastAPI: token + read APIs
app/
  db.py             SQLite connection + schema
  repository.py     users / appointments / summaries (double-booking guard)
  slots.py          hardcoded availability
  events.py         push tool-call + summary events to the UI
  summary.py        Gemini call-summary generation
  prompts.py        agent system instructions
```

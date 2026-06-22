# 📞 Call Flow — Mykare Front Desk Agent

How a patient call moves through the system, end to end.

Reference: [LiveKit Agents](https://docs.livekit.io/agents/) ·
[Avatar integrations](https://docs.livekit.io/agents/integrations/avatar/)

---

## 🔄 Flow

1. **Patient opens the web app**
   - Frontend calls `POST /api/token` on the FastAPI server.
   - FastAPI returns a LiveKit token + room name + server URL.

2. **Patient joins the LiveKit room**
   - The browser connects over WebRTC and publishes its microphone.
   - The **agent worker auto-dispatches** into the same room and starts the
     `FrontDeskAgent` ("Mira").
   - If `BEY_API_KEY` + `BEY_AVATAR_ID` are set, a Beyond Presence **avatar**
     joins as a participant and lip-syncs to Mira's speech; otherwise the call
     is voice-only.

3. **Conversation (5+ turns, context maintained)**
   - STT (Deepgram) → LLM (Gemini) → TTS (Deepgram), with multilingual
     turn-detection for natural turn-taking and < 3–5s responses.
   - Mira greets the patient and helps them with appointments.

4. **Tool calls drive every action** — and each is shown on the UI
   - `identify_user` → confirm phone (unique patient ID), load history
   - `fetch_slots` → real availability (booked slots filtered out)
   - `book_appointment` → save to DB, **double-booking prevented**, confirm
   - `retrieve_appointments` → read back the patient's bookings
   - `cancel_appointment` / `modify_appointment` → manage existing bookings
   - Every call publishes a `tool_events` data message
     (`"Fetching slots…"`, `"Booked ✅ …"`) the frontend renders as chips.

5. **End of call**
   - Patient says goodbye → Mira calls `end_conversation`, **or** the patient
     disconnects → the worker detects it.
   - The agent generates a **summary** (recap, intent, preferences,
     appointment list, timestamp) with Gemini in well under 10 seconds.
   - The summary is **persisted** (`call_summaries`) and **pushed** to the UI
     over the `call_summary` data topic, and is also fetchable at
     `GET /api/summary/{room}`.

---

## 🧩 Data the agent extracts

Name · Phone number · Date · Time · Intent — collected through the
conversation and tool arguments, persisted in SQLite, and surfaced in the
end-of-call summary.

---

## 🖥 UI event channels (LiveKit data messages)

| Topic | Payload | Used for |
|---|---|---|
| `tool_events` | `{ type:"tool_call", tool, status, label, data? }` | live action chips |
| `call_summary` | `{ type:"summary", summary, intent, preferences, appointments, timestamp, ... }` | summary card |

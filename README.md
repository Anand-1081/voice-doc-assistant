# Voice AI Doctor Appointment Assistant

A caller speaks to an AI receptionist ("Aria") that checks doctor availability, books and cancels appointments. The caller is identified **only by the phone number they speak aloud**, and the agent reads it back for confirmation before every booking or cancellation. A browser dashboard runs the call, shows a live transcript, doctors/slots, and appointments that update in real time.

**LLM:** Google Gemini (`gemini-2.0-flash`, free tier) · **STT/TTS:** ElevenLabs · **Framework:** Pipecat · **VAD:** Silero (bonus)

## Pipeline description
Browser mic audio travels over WebRTC (Pipecat `SmallWebRTCTransport`) into the pipeline:

`Mic → Silero VAD → ElevenLabs STT → Gemini (function calling) → ElevenLabs TTS → Speaker`

Silero VAD segments speech and enables interruptions. Transcribed text is added to the conversation context and sent to Gemini, which has three tools: `check_doctor_availability`, `book_appointment`, `cancel_appointment`. Tool handlers run against an in-memory store (`backend/store.py`). The booking/cancel tools take a `phone_confirmed` flag that the model may only set after reading the number back and hearing "yes"; the backend rejects calls without it, so the confirmation rule is enforced in code, not just in the prompt. Spoken digits ("nine eight seven…") are normalised to digits. Every tool call and result, plus user/agent transcripts, is pushed to the dashboard through a Server-Sent Events stream (`/api/events`), so the UI updates live and highlights affected rows.

## Setup
Requires Python 3.10+.
```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp ../.env.example ../.env    # then fill in GOOGLE_API_KEY and ELEVENLABS_API_KEY
python main.py
```
Open http://localhost:8000, click **Start call**, allow the microphone, and talk. (`localhost` is a secure context, so mic access works without HTTPS.)

## Demo script
1. "I'd like to see a cardiologist tomorrow." → availability check
2. "Book 10 AM." → agent asks for your phone number, reads it back, you say yes
3. Watch the appointment appear on the dashboard (and the confirmation card in the call panel)
4. "Cancel my appointment." → number read-back → row disappears

## Features
Required: voice pipeline, 3 tools, phone read-back confirmation, start/end call, live transcript, doctors/slots, live appointments.
Bonus: Silero VAD, call states (ringing/connected/ended) with timer, mute and end, live waveform, confirmation cards, row highlights on agent actions, ring/end sounds, responsive layout.

## Structure
```
backend/main.py      FastAPI: /api/offer (WebRTC), /api/state, /api/events (SSE), static UI
backend/bot.py       Pipecat pipeline, tool schemas, transcript tap
backend/store.py     Mock doctors/slots/appointments + tool logic + event bus
frontend/index.html  Dashboard (plain HTML/CSS/JS)
```

## Notes
- Data is in memory and resets on restart. Slots cover the next 5 days.
- Pipecat's API moves quickly; if an import fails, check the matching version in the Pipecat changelog (code written against ~0.0.9x).

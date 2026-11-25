import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv(Path(__file__).parent.parent / ".env")

import store  # noqa: E402
from bot import run_bot  # noqa: E402
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection  # noqa: E402

app = FastAPI(title="Voice Doctor Assistant")
ICE = ["stun:stun.l.google.com:19302"]


@app.post("/api/offer")
async def offer(request: Request, background: BackgroundTasks):
    body = await request.json()
    conn = SmallWebRTCConnection(ICE)
    await conn.initialize(sdp=body["sdp"], type=body["type"])
    background.add_task(run_bot, conn)
    return conn.get_answer()


@app.get("/api/state")
async def state():
    return store.snapshot()


@app.get("/api/events")
async def events():
    q = store.subscribe()

    async def stream():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            store.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "frontend", html=True), name="ui")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

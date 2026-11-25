"""Pipecat voice pipeline: Mic -> ElevenLabs STT -> Gemini (tools) -> ElevenLabs TTS -> Speaker."""
import os
from datetime import date

import aiohttp
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.elevenlabs.stt import ElevenLabsSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
import store

SYSTEM_PROMPT = f"""You are Aria, a warm, concise voice receptionist for CityCare Clinic. Today is {date.today().isoformat()}.
You help callers check doctor availability, book, and cancel appointments, all by voice.
Rules:
- Keep replies SHORT (1-2 sentences). No markdown, lists, or emojis; this is spoken audio. Say dates naturally ("Thursday, the 26th").
- The caller is identified ONLY by the phone number they speak. Never ask for OTP or ID.
- Before booking or cancelling, ask for the phone number, then READ IT BACK digit by digit and wait for an explicit yes. Only then call the tool with phone_confirmed=true. If they correct you, read back again.
- Use check_doctor_availability before offering times; never invent slots. Dates are ISO (YYYY-MM-DD) in tool calls, times are 24h HH:MM.
- After a tool result, state the outcome plainly (doctor, date, time) and ask if they need anything else.
- Doctors: Dr. Ananya Sharma (Cardiology), Dr. Rohan Mehta (Dermatology), Dr. Priya Nair (Pediatrics), Dr. Vikram Singh (Orthopedics)."""

PHONE = {
    "phone_number": {"type": "string", "description": "Phone number exactly as spoken by the caller"},
    "phone_confirmed": {"type": "boolean", "description": "True only after reading the number back and the caller said yes"},
}

TOOLS = ToolsSchema(standard_tools=[
    FunctionSchema(
        name="check_doctor_availability",
        description="List available appointment slots, optionally for one doctor/specialty and date.",
        properties={
            "doctor_name": {"type": "string"},
            "specialty": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
        },
        required=[],
    ),
    FunctionSchema(
        name="book_appointment",
        description="Book a slot for the caller.",
        properties={
            **PHONE,
            "doctor_name": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "time": {"type": "string", "description": "HH:MM 24h"},
            "patient_name": {"type": "string"},
        },
        required=["phone_number", "phone_confirmed", "doctor_name", "date", "time"],
    ),
    FunctionSchema(
        name="cancel_appointment",
        description="Cancel the caller's appointment.",
        properties={
            **PHONE,
            "appointment_id": {"type": "string", "description": "Only if caller has several"},
        },
        required=["phone_number", "phone_confirmed"],
    ),
])


class TranscriptTap(FrameProcessor):
    """Forwards user transcripts and assembled bot sentences to the dashboard."""

    def __init__(self):
        super().__init__()
        self._buf = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            store.publish({"type": "transcript", "role": "user", "text": frame.text.strip()})
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._buf = ""
        elif isinstance(frame, LLMTextFrame):
            self._buf += frame.text
        elif isinstance(frame, LLMFullResponseEndFrame) and self._buf.strip():
            store.publish({"type": "transcript", "role": "agent", "text": self._buf.strip()})
            self._buf = ""
        await self.push_frame(frame, direction)


async def run_bot(connection):
    async with aiohttp.ClientSession() as session:
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                vad_analyzer=SileroVADAnalyzer(),
            ),
        )

        stt = ElevenLabsSTTService(
            api_key=os.environ["ELEVENLABS_API_KEY"],
            aiohttp_session=session,
        )
        tts = ElevenLabsTTSService(
            api_key=os.environ["ELEVENLABS_API_KEY"],
            voice_id=os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
        )
        llm = GoogleLLMService(
            api_key=os.environ["GOOGLE_API_KEY"],
            model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        )

        async def _call(params: FunctionCallParams, fn):
            a = params.arguments
            store.publish({"type": "action", "action": "tool", "name": params.function_name, "args": a})
            await params.result_callback(fn(a))

        llm.register_function(
            "check_doctor_availability",
            lambda p: _call(p, lambda a: store.check_availability(a.get("doctor_name"), a.get("specialty"), a.get("date"))),
        )
        llm.register_function(
            "book_appointment",
            lambda p: _call(
                p,
                lambda a: store.book(
                    a.get("phone_number", ""),
                    a.get("phone_confirmed", False),
                    a.get("doctor_name"),
                    a.get("date"),
                    a.get("time"),
                    a.get("patient_name"),
                ),
            ),
        )
        llm.register_function(
            "cancel_appointment",
            lambda p: _call(
                p,
                lambda a: store.cancel(
                    a.get("phone_number", ""),
                    a.get("phone_confirmed", False),
                    a.get("appointment_id"),
                ),
            ),
        )

        context = LLMContext(messages=[{"role": "system", "content": SYSTEM_PROMPT}], tools=TOOLS)
        agg = LLMContextAggregatorPair(context)

        pipeline = Pipeline([
            transport.input(),
            stt,
            TranscriptTap(),
            agg.user(),
            llm,
            TranscriptTap(),
            tts,
            transport.output(),
            agg.assistant(),
        ])
        task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

        @transport.event_handler("on_client_connected")
        async def _connected(_t, _c):
            context.add_message({
                "role": "user",
                "content": "A caller just connected. Greet them briefly and ask how you can help.",
            })
            await task.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def _disconnected(_t, _c):
            await task.cancel()

        await PipelineRunner(handle_sigint=False).run(task)
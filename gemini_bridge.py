import asyncio
import base64
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from google import genai
from google.genai import types

from audio_utils import StreamResampler, resample_stream_async
from config import (
    GEMINI_API_KEY, GEMINI_MODEL, GEMINI_VOICE, AGENT_LANGUAGE,
    TRANSFER_NUMBER,
)
from prompts import build_system_prompt

logger = logging.getLogger(__name__)

_QUEUE_MAX = 200


def _build_tools() -> list:
    S = types.Schema
    decls = [
        types.FunctionDeclaration(
            name="end_call",
            description=("End the phone call. Call this exactly once, only AFTER you have spoken your "
                         "closing line, when the conversation is complete or the caller wants to disconnect."),
            parameters=S(type="OBJECT", properties={
                "reason": S(type="STRING", description="e.g. call_completed, caller_said_bye"),
            }, required=[]),
        ),
        types.FunctionDeclaration(
            name="transfer_call",
            description=("Transfer the call to a human. Speak one short handoff line first, then call this. "
                         "Do not call end_call afterwards."),
            parameters=S(type="OBJECT", properties={
                "destination": S(type="STRING", description="Phone number to transfer to."),
                "reason": S(type="STRING", description="e.g. wants_human, complaint, cannot_answer"),
            }, required=[]),
        ),
        types.FunctionDeclaration(
            name="capture_lead_info",
            description=("Record any detail the caller has just stated. Call it immediately each time the "
                         "caller gives something new. Include only fields stated in this turn. "
                         "Write names in Latin script. Never guess."),
            parameters=S(type="OBJECT", properties={
                "name": S(type="STRING", description="Caller's name"),
                "company": S(type="STRING", description="Caller's company / business"),
                "industry": S(type="STRING", description="Industry, e.g. real estate, education"),
                "use_case": S(type="STRING", description="What they want a voice agent for"),
                "monthly_volume": S(type="STRING", description="Rough monthly calls or leads"),
                "current_crm": S(type="STRING", description="CRM they use"),
                "languages": S(type="STRING", description="Languages they need"),
                "timeline": S(type="STRING", description="When they want to start"),
                "email": S(type="STRING", description="Email address"),
                "phone": S(type="STRING", description="Phone / WhatsApp number if different from caller ID"),
                "is_decision_maker": S(type="BOOLEAN", description="True if they decide alone"),
                "demo_requested": S(type="BOOLEAN", description="True if they want a demo"),
                "callback_time": S(type="STRING", description=(
                    "Callback/demo time as an absolute 'YYYY-MM-DD HH:MM:SS'. Convert relative days "
                    "(tomorrow, Friday, kal) using today's date from the instructions; default time 11:00:00.")),
            }, required=[]),
        ),
    ]
    return [types.Tool(function_declarations=decls)]


class GeminiBridge:
    def __init__(
        self,
        call_sid: str,
        prompt_type: str = "inbound",
        org_config: Optional[dict] = None,
        telephony: bool = True,
        on_text: Optional[Callable[[str, str], None]] = None,
    ):
        self.call_sid = call_sid
        self.prompt_type = prompt_type
        self.org_config = org_config
        self.telephony = telephony
        self._on_text = on_text          # optional live hook: on_text("agent"|"caller", text)

        self._up = StreamResampler(from_rate=8000, to_rate=16000)
        self._down = StreamResampler(from_rate=24000, to_rate=8000)

        self._client = self._make_client()
        self._ctx = None
        self._session = None
        self._task: Optional[asyncio.Task] = None
        self._active = False

        # audio going back to the caller (8 kHz if telephony, else raw 24 kHz)
        self.output_queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)

        # results the rest of the app reads
        self.transcript_turns: list[dict] = []
        self.lead_info: dict = {}
        self.call_should_end = False
        self.transfer_requested = False
        self.transfer_reason = ""
        self.transfer_destination = ""

        # barge-in: consumer of output_queue clears its own buffer when this is True, then resets it
        self._interrupted_flag = False
        self._generation = 0

        self._caller_has_spoken = False
        self._turns_done = 0
        self._last_activity = time.monotonic()
    

    # setup 
    def _make_client(self):
        return genai.Client(api_key=GEMINI_API_KEY, http_options={"api_version": "v1beta"})

    async def start(self, greeting: Optional[str] = None):
        now = datetime.now()
        system_prompt = build_system_prompt(self.prompt_type, self.org_config) + (
            f"\n\nToday is {now.strftime('%A, %Y-%m-%d %H:%M:%S')}. When you call capture_lead_info with "
            f"callback_time, resolve relative days against this date and use 'YYYY-MM-DD HH:MM:SS' "
            f"(default 11:00:00)."
        )

        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            tools=_build_tools(),
            system_instruction=types.Content(role="user", parts=[types.Part(text=system_prompt)]),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=GEMINI_VOICE)),
                language_code=AGENT_LANGUAGE,
            ),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=False)),
        )

        logger.info(f"[{self.call_sid}] Connecting to Gemini, model={GEMINI_MODEL}")
        self._ctx = self._client.aio.live.connect(model=GEMINI_MODEL, config=config)
        self._session = await self._ctx.__aenter__()
        self._active = True
        self._touch()
        self._task = asyncio.create_task(self._receive_loop())

        if greeting:
            await self._session.send_realtime_input(text=greeting)
        logger.info(f"[{self.call_sid}] Gemini session open.")

    async def stop(self):
        self._active = False
        try:
            self.output_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ctx:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass
        logger.info(f"[{self.call_sid}] Gemini session closed.")

    #  caller audio in
    async def send_audio(self, pcm: bytes):
        """telephony: 8 kHz PCM16 from the phone. terminal: 16 kHz PCM16 from the mic."""
        if not self._active or not self._session:
            return
        if self.telephony:
            pcm = await resample_stream_async(self._up, pcm)
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))

    #  helpers 
    def _touch(self):
        self._last_activity = time.monotonic()

    def seconds_since_activity(self) -> float:
        """Seconds since the caller last spoke or the agent last produced audio."""
        return time.monotonic() - self._last_activity

    def can_end_call(self) -> bool:
        return self._caller_has_spoken or self._turns_done >= 2

    def _add_text(self, speaker: str, text: str):
        if not text:
            return
        if self.transcript_turns and self.transcript_turns[-1]["speaker"] == speaker:
            self.transcript_turns[-1]["text"] += text
        else:
            self.transcript_turns.append(
                {"speaker": speaker, "text": text, "timestamp": datetime.now(timezone.utc).isoformat()})
        if self._on_text:
            try:
                self._on_text(speaker, text)
            except Exception:
                pass

    def full_transcript(self) -> str:
        return "\n".join(f"{t['speaker']}: {t['text']}" for t in self.transcript_turns)

    def _on_interrupted(self):
        self._generation += 1
        drained = 0
        while True:
            try:
                self.output_queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
        self._interrupted_flag = True
        self._down.reset()
        logger.info(f"[{self.call_sid}] Caller interrupted, dropped {drained} queued chunk(s).")

    async def _on_agent_audio(self, data):
        raw = base64.b64decode(data) if isinstance(data, str) else bytes(data)
        pcm = await resample_stream_async(self._down, raw) if self.telephony else raw
        self._touch()
        try:
            self.output_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            logger.warning(f"[{self.call_sid}] Output queue full, dropping audio chunk.")

    #  Gemini
    async def _receive_loop(self):
        try:
            while self._active:
                gen = self._generation            # audio from an older generation is stale
                async for r in self._session.receive():
                    if not self._active:
                        break
                    sc = r.server_content

                    if sc and sc.interrupted:
                        self._on_interrupted()
                        break                      # start a fresh receive() so new audio is not dropped

                    if r.data and gen == self._generation:
                        await self._on_agent_audio(r.data)

                    if sc:
                        ot = sc.output_transcription
                        if ot and ot.text:
                            self._add_text("agent", ot.text)
                        it = sc.input_transcription
                        if it and it.text and it.text.strip():
                            self._caller_has_spoken = True
                            self._touch()
                            self._add_text("caller", it.text)
                        if sc.turn_complete:
                            self._turns_done += 1

                    if r.tool_call:
                        await self._handle_tools(r.tool_call)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(f"[{self.call_sid}] Gemini receive error")
        finally:
            try:
                self.output_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def _handle_tools(self, tool_call):
        responses = []
        for fn in tool_call.function_calls:
            args = dict(fn.args or {})

            if fn.name == "end_call":
                if self.can_end_call():
                    logger.info(f"[{self.call_sid}] end_call ({args.get('reason', '')})")
                    self.call_should_end = True
                else:
                    logger.warning(f"[{self.call_sid}] end_call ignored: caller has not spoken yet.")
                result = {"result": "ok"}

            elif fn.name == "transfer_call":
                self.transfer_requested = True
                self.transfer_reason = args.get("reason") or "transfer_requested"
                self.transfer_destination = args.get("destination") or TRANSFER_NUMBER
                logger.info(f"[{self.call_sid}] transfer_call -> {self.transfer_destination} "
                            f"({self.transfer_reason})")
                result = {"result": "transferring"}

            elif fn.name == "capture_lead_info":
                clean = {k: v for k, v in args.items() if v not in (None, "")}
                if "callback_time" in clean:
                    clean["callback_time"] = _normalize_datetime(clean["callback_time"])
                self.lead_info.update(clean)
                logger.info(f"[{self.call_sid}] lead_info -> {self.lead_info}")
                result = {"result": "ok"}

            else:
                logger.warning(f"[{self.call_sid}] Unknown tool {fn.name!r}")
                result = {"error": "unknown tool"}

            responses.append(types.FunctionResponse(id=fn.id, name=fn.name, response=result))

        try:
            await self._session.send_tool_response(function_responses=responses)
        except Exception as e:
            logger.warning(f"[{self.call_sid}] Could not send tool response: {e}")



def _normalize_datetime(raw: str) -> str:
    """Safety net: force callback_time into 'YYYY-MM-DD HH:MM:SS' if the model gave something else."""
    try:
        from dateutil import parser as dtparser
        return dtparser.parse(raw, fuzzy=True, default=datetime.now().replace(hour=11, minute=0, second=0, microsecond=0)
                              ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return raw
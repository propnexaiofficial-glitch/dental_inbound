import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from gemini_bridge import GeminiBridge
from prompts import build_greeting_trigger

logger = logging.getLogger(__name__)

CHUNK_BYTES = 320            # 20 ms of 8 kHz PCM16
BYTES_PER_MS = 16.0
LOOKAHEAD_S = 0.2            # keep ~200 ms of audio buffered at Bonvoice
SILENCE_TIMEOUT_S = 20       # no caller speech and no agent audio for this long -> hang up
SILENCE_CHECK_S = 2
END_IDLE_S = 1.5             # after end_call, wait this long with no new audio before hanging up
TRANSFER_GRACE_S = 3.0


class WsCallHandler:
    def __init__(
        self,
        websocket: WebSocket,
        on_call_end: Optional[Callable[[dict], Awaitable[None]]] = None,
        prompt_type: str = "inbound",
        lead_name: str = "there",
        is_outbound: bool = False,
    ):
        self.ws = websocket
        self._on_call_end = on_call_end
        self._prompt_type = "outbound" if is_outbound and prompt_type == "inbound" else prompt_type
        self._lead_name = lead_name

        self.call_id = f"unknown-{uuid.uuid4().hex[:12]}"
        self.bridge: Optional[GeminiBridge] = None
        self._stream_id = ""
        self._caller_number = ""
        self._started_at: Optional[datetime] = None

        self._started = False
        self._packet_id = 0
        self._leftover = b""

        self._play_start: Optional[float] = None
        self._play_ms = 0.0
        self._last_chunk_at = time.monotonic()

        self._transfer_sent_at: Optional[float] = None
        self._sender_task: Optional[asyncio.Task] = None
        self._silence_task: Optional[asyncio.Task] = None

    # ── small helpers ────────────────────────────────────────────────────────
    def _connected(self) -> bool:
        return self.ws.client_state == WebSocketState.CONNECTED

    async def _send_json(self, payload: dict):
        try:
            await self.ws.send_text(json.dumps(payload))
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def _send_media(self, pcm: bytes):
        pcm = self._leftover + pcm
        usable = len(pcm) // CHUNK_BYTES * CHUNK_BYTES
        self._leftover = pcm[usable:]
        for i in range(0, usable, CHUNK_BYTES):
            self._packet_id += 1
            await self.ws.send_text(json.dumps({
                "event": "media",
                "stream_id": self._stream_id,
                "media": {
                    "packet_id": self._packet_id,
                    "timestamp": int(time.time() * 1000),
                    "payload": base64.b64encode(pcm[i:i + CHUNK_BYTES]).decode("ascii"),
                },
            }))

    def _reset_playback(self):
        self._play_start = None
        self._play_ms = 0.0
        self._leftover = b""

    def _remaining_playback_s(self) -> float:
        if self._play_start is None:
            return 0.0
        return max(0.0, self._play_start + self._play_ms / 1000.0 - time.monotonic())

    async def _hang_up(self):
        await self._send_json({"event": "stop", "call_id": self.call_id})
        try:
            if self._connected():
                await self.ws.close()
        except Exception:
            pass

    # ── main loop: Bonvoice -> Gemini ────────────────────────────────────────
    async def run(self):
        try:
            while True:
                try:
                    raw = await self.ws.receive_text()
                except (WebSocketDisconnect, RuntimeError):
                    logger.info(f"[{self.call_id}] WebSocket closed.")
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event = msg.get("event")
                if event == "start":
                    await self._handle_start(msg)
                elif event == "media":
                    if not (self._started and self.bridge):
                        continue
                    payload = (msg.get("media") or {}).get("payload", "")
                    if payload:
                        try:
                            await self.bridge.send_audio(base64.b64decode(payload))
                        except Exception:
                            logger.warning(f"[{self.call_id}] Bad audio payload.")
                elif event == "stop":
                    logger.info(f"[{self.call_id}] Stop event received.")
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"[{self.call_id}] Unexpected error")
        finally:
            await self._cleanup()

    async def _handle_start(self, msg: dict):
        if self._started:
            return
        data = msg.get("data") or {}

        if data.get("stream_id") not in (None, ""):
            self._stream_id = str(data["stream_id"])
        if data.get("call_id") not in (None, ""):
            self.call_id = str(data["call_id"])
        self._caller_number = str(data.get("from") or msg.get("caller_number") or "")

        self._lead_name = msg.get("lead_name") or self._lead_name
        self._prompt_type = msg.get("prompt_type") or self._prompt_type
        self._started_at = datetime.now(timezone.utc)

        self.bridge = GeminiBridge(self.call_id, prompt_type=self._prompt_type, telephony=True)
        await self.bridge.start(greeting=build_greeting_trigger(self._prompt_type, self._lead_name))

        self._started = True
        self._sender_task = asyncio.create_task(self._audio_sender())
        self._silence_task = asyncio.create_task(self._silence_watcher())
        logger.info(f"[{self.call_id}] Call started (prompt_type={self._prompt_type}, "
                    f"caller={self._caller_number or 'unknown'}).")

    # ── Gemini -> Bonvoice ───────────────────────────────────────────────────
    async def _audio_sender(self):
        b = self.bridge
        try:
            while True:
                if b._interrupted_flag:
                    b._interrupted_flag = False
                    await self._send_json({"event": "clear", "stream_id": self._stream_id})
                    self._reset_playback()

                try:
                    chunk = await asyncio.wait_for(b.output_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    idle = time.monotonic() - self._last_chunk_at
                    remaining = self._remaining_playback_s()
                    self._play_start, self._play_ms = None, 0.0

                    if b.transfer_requested and self._transfer_sent_at is None and idle >= END_IDLE_S:
                        await asyncio.sleep(remaining)          # let the handoff line finish playing
                        target = "".join(ch for ch in b.transfer_destination if ch.isdigit())
                        if target:
                            await self._send_json({"event": "transfer", "transferTo": target})
                            logger.info(f"[{self.call_id}] Transfer sent -> {target}")
                        self._transfer_sent_at = time.monotonic()
                        continue
                    if self._transfer_sent_at is not None:
                        if time.monotonic() - self._transfer_sent_at >= TRANSFER_GRACE_S:
                            await self._hang_up()
                            break
                        continue
                    if b.call_should_end and idle >= END_IDLE_S:
                        await asyncio.sleep(remaining)          # let the closing line finish playing
                        logger.info(f"[{self.call_id}] Call ending after closing line.")
                        await self._hang_up()
                        break
                    continue

                if chunk is None:
                    break

                if b._interrupted_flag:                         # stale chunk from before the barge-in
                    continue

                # pace audio so Bonvoice's buffer is not flooded
                now = time.monotonic()
                if self._play_start is None:
                    self._play_start, self._play_ms = now, 0.0
                wait = self._play_start + self._play_ms / 1000.0 - now - LOOKAHEAD_S
                if wait > 0:
                    await asyncio.sleep(wait)
                self._play_ms += len(chunk) / BYTES_PER_MS

                if not self._connected():
                    break
                try:
                    await self._send_media(chunk)
                except (WebSocketDisconnect, RuntimeError):
                    break
                self._last_chunk_at = time.monotonic()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(f"[{self.call_id}] Audio sender error")

    async def _silence_watcher(self):
        try:
            while True:
                await asyncio.sleep(SILENCE_CHECK_S)
                if not self._connected() or self.bridge.call_should_end:
                    break
                if self.bridge.seconds_since_activity() >= SILENCE_TIMEOUT_S:
                    logger.info(f"[{self.call_id}] Silence timeout, ending call.")
                    await self._hang_up()
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(f"[{self.call_id}] Silence watcher error")

    # ── cleanup ──────────────────────────────────────────────────────────────
    async def _cleanup(self):
        for task in (self._sender_task, self._silence_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        record = {
            "call_id": self.call_id,
            "caller_number": self._caller_number,
            "prompt_type": self._prompt_type,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "transfer_requested": bool(self.bridge and self.bridge.transfer_requested),
            "transfer_reason": self.bridge.transfer_reason if self.bridge else "",
            "lead_info": dict(self.bridge.lead_info) if self.bridge else {},
            "transcript": list(self.bridge.transcript_turns) if self.bridge else [],
        }

        if self.bridge:
            await self.bridge.stop()
        try:
            if self._connected():
                await self.ws.close()
        except Exception:
            pass

        if self._on_call_end and self._started:
            try:
                await self._on_call_end(record)
            except Exception:
                logger.exception(f"[{self.call_id}] on_call_end failed")
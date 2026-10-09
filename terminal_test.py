import asyncio
import json
import logging
import os
import sys
import threading
from datetime import datetime

import numpy as np
import sounddevice as sd

sys.path.insert(0, os.path.dirname(__file__))

from config import AGENT_NAME, COMPANY_NAME, GEMINI_MODEL, GEMINI_VOICE
from gemini_bridge import GeminiBridge
from prompts import build_greeting_trigger

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(name)s | %(message)s")

MIC_RATE = 16_000          # Gemini input
OUT_RATE = 24_000          # Gemini output
MIC_BLOCK = 320            # 20 ms @ 16 kHz
OUT_BLOCK = 480            # 20 ms @ 24 kHz

CYAN, GREEN, YELLOW, BOLD, RESET = "\033[96m", "\033[92m", "\033[93m", "\033[1m", "\033[0m"


class LivePrinter:
    """Prints transcript text as it streams in, with a label whenever the speaker changes."""
    def __init__(self):
        self._last = None

    def __call__(self, speaker: str, text: str):
        if speaker != self._last:
            label = f"{AGENT_NAME}" if speaker == "agent" else "You"
            colour = CYAN if speaker == "agent" else GREEN
            print(f"\n{colour}{BOLD}{label}:{RESET} ", end="", flush=True)
            self._last = speaker
        colour = CYAN if speaker == "agent" else GREEN
        print(f"{colour}{text}{RESET}", end="", flush=True)


async def run():
    prompt_type, lead_name = "inbound", "there"   # one agent for every call
    print(f"\n{BOLD}{'-' * 60}{RESET}")
    print(f"{BOLD}  Voice Agent Terminal Test{RESET}")
    print(f"  Agent: {CYAN}{AGENT_NAME}{RESET} ({COMPANY_NAME})   Voice: {GEMINI_VOICE}")
    print(f"  Model: {YELLOW}{GEMINI_MODEL}{RESET}   Voice: {GEMINI_VOICE}")
    print(f"{BOLD}{'-' * 60}{RESET}")
    print(f"  {YELLOW}Use headphones. Speak when the agent finishes. Ctrl+C to end.{RESET}")

    loop = asyncio.get_running_loop()
    bridge = GeminiBridge("terminal-test", prompt_type=prompt_type, telephony=False,
                          on_text=LivePrinter())

    speaker_buf = bytearray()
    buf_lock = threading.Lock()

    def out_callback(outdata, frames, time_info, status):
        need = frames * 2
        with buf_lock:
            if bridge._interrupted_flag:           # caller barged in: drop buffered agent audio
                speaker_buf.clear()
                bridge._interrupted_flag = False
            chunk = bytes(speaker_buf[:need])
            del speaker_buf[:need]
        chunk += bytes(need - len(chunk))
        outdata[:, 0] = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0

    mic_q: asyncio.Queue = asyncio.Queue(maxsize=100)

    def _push(pcm: bytes):
        try:
            mic_q.put_nowait(pcm)
        except asyncio.QueueFull:
            pass

    def in_callback(indata, frames, time_info, status):
        loop.call_soon_threadsafe(_push, indata.tobytes())

    await bridge.start(greeting=build_greeting_trigger(prompt_type, lead_name))

    out_stream = sd.OutputStream(samplerate=OUT_RATE, channels=1, dtype="float32",
                                 blocksize=OUT_BLOCK, callback=out_callback)
    in_stream = sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="int16",
                               blocksize=MIC_BLOCK, callback=in_callback)
    out_stream.start()
    in_stream.start()
    print(f"  {GREEN}Connected. Listening...{RESET}")

    async def mic_sender():
        while True:
            await bridge.send_audio(await mic_q.get())

    async def speaker_feeder():
        while True:
            chunk = await bridge.output_queue.get()
            if chunk is None:
                break
            with buf_lock:
                speaker_buf.extend(chunk)

    tasks = [asyncio.create_task(mic_sender()), asyncio.create_task(speaker_feeder())]

    try:
        while True:
            await asyncio.sleep(0.2)
            if bridge.call_should_end:
                while bridge.output_queue.qsize() or len(speaker_buf):   # let the closing line finish
                    await asyncio.sleep(0.1)
                await asyncio.sleep(1.0)
                print(f"\n\n  {YELLOW}[Agent ended the call]{RESET}")
                break
            if bridge.transfer_requested:
                await asyncio.sleep(4)
                print(f"\n\n  {YELLOW}[Agent requested transfer to {bridge.transfer_destination}]{RESET}")
                break
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        in_stream.stop(); in_stream.close()
        out_stream.stop(); out_stream.close()
        await bridge.stop()

        print(f"\n{BOLD}{'-' * 60}{RESET}")
        print(f"{BOLD}  Captured lead info{RESET}")
        print(json.dumps(bridge.lead_info, indent=2, ensure_ascii=False) if bridge.lead_info else "  (none)")
        os.makedirs("transcripts", exist_ok=True)
        path = os.path.join("transcripts", f"terminal_{datetime.now():%Y%m%d_%H%M%S}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"lead_info": bridge.lead_info, "transcript": bridge.transcript_turns},
                      f, ensure_ascii=False, indent=2)
        print(f"  Saved: {path}\n")


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print(f"\n  {YELLOW}Session ended.{RESET}\n")


if __name__ == "__main__":
    main()
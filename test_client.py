import asyncio
import base64
import json
import math
import os
import struct
import sys
import time
import uuid
import wave

import websockets


DEFAULT_URL = "ws://localhost:8100/ws/voice-agent"

if len(sys.argv) > 1:
    SERVER_URL = sys.argv[1]
else:
    SERVER_URL = os.getenv("SERVER_URL", DEFAULT_URL)

# Bonvoice's requirements
SAMPLE_RATE = 8000          
SAMPLE_WIDTH = 2            # bytes (16-bit)
CHANNELS = 1                # mono
CHUNK_BYTES = 320           # MUST be a multiple of 320, per spec


def generate_test_audio_pcm16(freq=300, duration_sec=1.0, sample_rate=SAMPLE_RATE) -> bytes:
    """Synthetic sine-wave tone as raw linear PCM16 (little-endian).
    Real callers obviously sound different - this is just to exercise
    the pipeline end-to-end."""
    n_samples = int(sample_rate * duration_sec)
    samples = []
    for i in range(n_samples):
        val = int(5000 * math.sin(2 * math.pi * freq * i / sample_rate))
        samples.append(struct.pack("<h", val))
    return b"".join(samples)


async def run_test():
    print(f"-> Connecting to {SERVER_URL} ...")

    stream_id = str(uuid.uuid4())
    call_id = "test-call-bonvoice-001"

    replies_received = 0
    reply_pcm_chunks = []
    packet_id_counter = 0

    try:
        async with websockets.connect(SERVER_URL) as ws:
            print("OK  WebSocket connected")

            #  Call Start Event 
            start_msg = {
                "event": "start",
                "data": {
                    "stream_id": stream_id,
                    "call_id": call_id,
                    "from": "8305989380",   # caller_id
                    "to": "1800XXXXXXX",    # your Bonvoice virtual number (DID)
                },
            }
            await ws.send(json.dumps(start_msg))
            print(f"-> Sent 'start' event: {start_msg}")

            async def listen_for_replies():
                nonlocal replies_received
                try:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            print(f"  [WARN] Non-JSON message received: {raw[:200]}")
                            continue

                        event = msg.get("event")

                        if event == "media":
                            media = msg.get("media", {})
                            audio_b64 = media.get("payload")

                            if not audio_b64:
                                print(f"  [WARN] 'media' event with no payload: {msg}")
                                continue

                            try:
                                audio_bytes = base64.b64decode(audio_b64)
                            except Exception as e:
                                print(f"  [WARN] Failed to decode audio: {e}")
                                continue

                        
                            if len(audio_bytes) % CHUNK_BYTES != 0:
                                print(f"  [WARN] Reply chunk size {len(audio_bytes)} "
                                      f"is NOT a multiple of {CHUNK_BYTES} bytes!")

                            reply_pcm_chunks.append(audio_bytes)  # already PCM16
                            replies_received += 1
                            print(f"<- Reply audio #{replies_received} received "
                                  f"({len(audio_bytes)} bytes, PCM16) "
                                  f"packet_id={media.get('packet_id')} "
                                  f"ts={media.get('timestamp')}")

                        elif event == "clear":
                            print(f"  [INFO] Received 'clear' (interruption) event "
                                  f"for stream_id={msg.get('stream_id')}")

                        elif event == "transfer":
                            print(f"  [INFO] Received 'transfer' event -> "
                                  f"{msg.get('transferTo')}")

                        else:
                            print(f"  [INFO] Non-media event received: {event!r} -> {msg}")

                except websockets.exceptions.ConnectionClosed:
                    print("  (connection closed by server)")
                except Exception as e:
                    print(f"  [LISTENER ERROR] {type(e).__name__}: {e}")

            listener = asyncio.create_task(listen_for_replies())

            await asyncio.sleep(1.0)

            #  Media packets
            test_audio = generate_test_audio_pcm16(duration_sec=1.0)

        
            usable_len = (len(test_audio) // CHUNK_BYTES) * CHUNK_BYTES
            test_audio = test_audio[:usable_len]

            for i in range(0, len(test_audio), CHUNK_BYTES):
                chunk = test_audio[i:i + CHUNK_BYTES]
                packet_id_counter += 1
                media_msg = {
                    "event": "media",
                    "stream_id": stream_id,
                    "media": {
                        "packet_id": packet_id_counter,
                        "timestamp": int(time.time() * 1000),
                        "payload": base64.b64encode(chunk).decode("ascii"),
                    },
                }
                await ws.send(json.dumps(media_msg))
               
                await asyncio.sleep(0.02)

            print(f"-> Sent {len(test_audio)} bytes of caller audio "
                  f"({packet_id_counter} packets of {CHUNK_BYTES} bytes each)")

            print("-> Waiting for AI reply (up to 6s)...")
            await asyncio.sleep(6.0)

            listener.cancel()

    except ConnectionRefusedError:
        print("FAILED: Server not reachable. Check the URL, the VPS firewall "
              "(port 8100) and that the service is running.")
        sys.exit(1)
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        sys.exit(1)

     
    if reply_pcm_chunks:
        full_pcm = b"".join(reply_pcm_chunks)
        out_path = "test_reply_output.wav"
        with wave.open(out_path, "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(full_pcm)
        print(f"\n-> Saved decoded reply audio to {out_path} - listen to it "
              f"to confirm audio quality.")

    print("\n" + "=" * 50)
    if replies_received > 0:
        print(f"TEST PASSED - {replies_received} reply audio chunk(s) received.")
        print("   WebSocket layer and pipeline work.")
        print("   Listen to test_reply_output.wav to verify audio quality.")
    else:
        print("TEST FAILED - no reply audio received.")
        print("Check server logs on the VPS:  journalctl -u propnex -n 50 --no-pager")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(run_test())
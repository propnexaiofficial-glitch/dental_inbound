import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from config import (
    SERVER_HOST, SERVER_PORT, ALLOWED_ORIGINS,
    AGENT_NAME, COMPANY_NAME, GEMINI_MODEL, GEMINI_API_KEY,
)
from ws_call_handler import WsCallHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("main")

TRANSCRIPT_DIR = os.path.join(os.path.dirname(__file__), "transcripts")
os.makedirs(TRANSCRIPT_DIR, exist_ok=True)

app = FastAPI(title=f"{COMPANY_NAME} Voice Agent", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


async def save_call(record: dict):
    """Called once when a call ends. Writes one JSON file per call (transcript + lead info)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_id = re.sub(r'[<>:"/\\|?*\s]', "_", str(record["call_id"]))
    path = os.path.join(TRANSCRIPT_DIR, f"{ts}_{safe_id}.json")

    def _write():
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    await asyncio.to_thread(_write)
    logger.info(f"[{record['call_id']}] Call saved -> {path}")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent": AGENT_NAME,
        "company": COMPANY_NAME,
        "model": GEMINI_MODEL,
        "gemini_configured": bool(GEMINI_API_KEY),
    }


@app.websocket("/ws/voice-agent")
async def voice_agent_ws(websocket: WebSocket):
    await websocket.accept()
    qp = websocket.query_params
    logger.info(f"WebSocket connected from {websocket.client}")
    handler = WsCallHandler(
        websocket,
        on_call_end=save_call,
        prompt_type=qp.get("prompt_type", "inbound"),
        lead_name=qp.get("lead_name", "there"),
        is_outbound=qp.get("is_outbound", "false").lower() == "true",
    )
    await handler.run()


if __name__ == "__main__":
    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=False, log_level="info")
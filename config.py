import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "models/gemini-3.8-live")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "leda")


# Agent persona 
AGENT_NAME = os.getenv("AGENT_NAME", "Riya")
COMPANY_NAME = os.getenv("COMPANY_NAME", "SmileCare Dental Clinic")
AGENT_LANGUAGE = os.getenv("AGENT_LANGUAGE", "en-IN")

#  Server 
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("PORT") or os.getenv("SERVER_PORT", "8000"))   # Render sets PORT

_origins = os.getenv("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = ["*"] if _origins == "*" else [o.strip() for o in _origins.split(",") if o.strip()]

# Features
TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER", "")
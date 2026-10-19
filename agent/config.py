"""Settings. Everything can be overridden in the .env file."""
from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:  # dotenv is optional
    pass

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Free-tier Gemini models, tried in order until one works.
# Set GEMINI_MODEL in .env to force a specific one.
MODEL_CANDIDATES = [
    m
    for m in [
        os.getenv("GEMINI_MODEL", "").strip(),
        "gemini-3.6-flash",  # confirmed working on the free key
        "gemini-3.8-flash",
        "gemini-3-flash-preview",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    if m
]

# Second free provider (optional but recommended): Groq, OpenAI-compatible API.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODELS = [
    m
    for m in [os.getenv("GROQ_MODEL", "").strip(), "openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
    if m
]
GROQ_TPM = int(os.getenv("GROQ_TPM", "7500"))  # free tier allows 8K tokens/minute; stay just under it
GROQ_MIN_INTERVAL = float(os.getenv("GROQ_MIN_INTERVAL", "2.5"))
GROQ_MAX_OUTPUT = int(os.getenv("GROQ_MAX_OUTPUT", "3000"))
GROQ_DOC_CHARS = int(os.getenv("GROQ_DOC_CHARS", "8000"))  # documents sent to Groq are trimmed to fit its limit

# Pacing so the free tier doesn't rate-limit us (seconds between calls).
LLM_MIN_INTERVAL = float(os.getenv("LLM_MIN_INTERVAL", "6"))
SEARCH_MIN_INTERVAL = float(os.getenv("SEARCH_MIN_INTERVAL", "3"))

# Saved next to the code, whichever folder the command is run from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.getenv("CACHE_DIR", os.path.join(ROOT, ".cache"))
REPORT_DIR = os.getenv("REPORT_DIR", os.path.join(ROOT, "reports"))
USE_CACHE = True

# How much work a run does. "quick" is for rehearsals and tight demos.
PROFILES = {
    "full": {"docs": 14, "peer_docs": 3, "candidates": 3, "redteam_docs": 4, "doc_chars": 20000, "batch_chars": 70000},
    "quick": {"docs": 8, "peer_docs": 2, "candidates": 2, "redteam_docs": 2, "doc_chars": 14000, "batch_chars": 60000},
}
PROFILE = PROFILES["full"]

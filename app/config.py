"""Settings read from the environment (and .env at the repo root)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # trust the OS certificate store too (networks with a TLS-inspecting proxy break certifi-only clients)
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or ""
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
MODEL = os.getenv("MODEL") or "openai/gpt-4.1-mini"
# USD per million tokens, used for the usage.cost_usd estimate (gpt-4.1-mini list price)
PRICE_IN = float(os.getenv("PRICE_IN_PER_M") or 0.40)
PRICE_OUT = float(os.getenv("PRICE_OUT_PER_M") or 1.60)

# Where the per-session conversation memory is kept between restarts (short-term memory)
SESSIONS_FILE = Path(os.getenv("SESSIONS_FILE") or ROOT / "data" / "sessions.json")

logging.basicConfig(level=os.getenv("LOG_LEVEL") or "INFO",
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("receptionist")

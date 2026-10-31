"""Signed-in users of the UI: name per phone number and their dated message history.

Kept in data/users.json, separate from the clinic's patient records: signing in never writes to the
clinic. A user's conversation runs in the agent session "user-<phone>", so it survives a new browser tab.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from .config import ROOT, log

USERS_FILE = Path(os.getenv("USERS_FILE") or ROOT / "data" / "users.json")
MAX_MESSAGES = 2000  # per user
_lock = threading.Lock()

_NAME_RX = [
    re.compile(r"\b(?:my name is|my name's|name is|i am|i'm|this is|it's)\s+([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,3})", re.I),
    re.compile(r"(?:اسمي|إسمي|معك|انا|أنا)\s+([ء-ي]+(?:\s+[ء-ي]+){0,3})"),
]
_NAME_STOP = {"and", "my", "phone", "number", "from", "calling", "here", "please", "with", "at", "on"}
_NOT_NAMES = {"calling", "here", "looking", "trying", "a", "an", "the", "new", "patient", "booking", "fine", "ok"}


def _load() -> dict:
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        USERS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save users: %s", exc)


def session_id(phone: str) -> str:
    return f"user-{phone}"


def phone_of(sid: str) -> str | None:
    return sid[5:] if sid.startswith("user-") else None


def find_name(text: str, name_only: bool = False) -> str | None:
    """A name the caller gave ("my name is Sara Khalil", "اسمي سارة"). With name_only, a short
    digit-free answer ("Sara Khalil") counts as the name itself."""
    t = (text or "").strip()
    for rx in _NAME_RX:
        m = rx.search(t)
        if m and m.group(1).split()[0].lower() not in _NOT_NAMES:
            words = []
            for w in m.group(1).split():  # "Sara Khalil and my number is" -> "Sara Khalil"
                if w.lower() in _NAME_STOP or w in ("ورقمي", "رقمي", "رقم", "و"):
                    break
                words.append(w)
            name = " ".join(words)
            return name.title() if name.isascii() else name
    words = re.sub(r"[^\w\s'\-]", " ", t).split()
    if name_only and 1 <= len(words) <= 4 and not re.search(r"\d", t):
        return " ".join(words).title() if t.isascii() else " ".join(words)
    return None


def get(phone: str) -> dict | None:
    with _lock:
        return _load().get(phone)


def upsert(phone: str, name: str) -> None:
    with _lock:
        data = _load()
        data.setdefault(phone, {"messages": []})["name"] = name
        _save(data)


def log_turn(phone: str, user_text: str, reply: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        data = _load()
        u = data.setdefault(phone, {"name": None, "messages": []})
        if user_text:
            u["messages"].append({"ts": now, "role": "user", "content": user_text})
        u["messages"].append({"ts": now, "role": "assistant", "content": reply})
        u["messages"] = u["messages"][-MAX_MESSAGES:]
        _save(data)


def history(phone: str) -> list[dict]:
    with _lock:
        return list((_load().get(phone) or {}).get("messages") or [])

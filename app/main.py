"""FastAPI entry point: the Ship Week API contract (/health, /api/chat, /api/approve, /api/voice).

    uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import agent, users
from .approval import decide
from .config import MODEL, OPENROUTER_API_KEY, SESSIONS_FILE, log
from .guards import find_phone
from .mcp_client import MCP


# ---------------------------------------------------------------- short-term memory (sessions)
class Sessions:
    """Conversation state per session_id, saved to a JSON file so a restart does not forget it."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        try:
            self.data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def get(self, sid: str) -> dict:
        if sid not in self.data:
            self.data[sid] = agent.new_session()
        return self.data[sid]

    def save(self) -> None:
        try:
            SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            recent = dict(list(self.data.items())[-500:])
            SESSIONS_FILE.write_text(json.dumps(recent, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning("could not save sessions: %s", exc)


SESSIONS = Sessions()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not OPENROUTER_API_KEY:
        log.warning("OPENROUTER_API_KEY is empty: the intake agent cannot run")
    await MCP.start()
    yield
    await MCP.stop()
    SESSIONS.save()


app = FastAPI(title="Zaytoun Clinic Voice Receptionist", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class SpeakIn(BaseModel):
    text: str = Field("", max_length=4000)


class ChatIn(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=120)
    message: str = Field("", max_length=4000)
    user: dict[str, Any] | None = None


class LoginIn(BaseModel):
    text: str = Field("", max_length=500)
    phone: str | None = None  # the number from an earlier attempt, when we only asked for the name


class ApproveIn(BaseModel):
    session_id: str
    action_id: str
    approved: bool


def _history(session: dict, user: str, reply: str) -> None:
    session["history"] = (session["history"] + [{"role": "user", "content": user},
                                                {"role": "assistant", "content": reply}])[-12:]


def _response(sid: str, out: dict, t0: float) -> dict:
    ms = int((time.perf_counter() - t0) * 1000)
    log.info("turn %s state=%s pending=%s result=%s %dms tools=%s", sid, out.get("state"),
             (out.get("pending_action") or {}).get("type"), out.get("result"), ms,
             ",".join(c["tool"] for c in out.get("tool_calls", [])))
    return {"session_id": sid, "reply": out["reply"], "state": out.get("state"),
            "pending_action": out.get("pending_action"), "result": out.get("result"), "citations": [],
            "tool_calls": out.get("tool_calls", []), "usage": out.get("usage"), "latency_ms": ms}


async def handle_chat(sid: str, message: str) -> dict:
    t0 = time.perf_counter()
    async with SESSIONS.locks[sid]:
        session = SESSIONS.get(sid)
        try:
            out = await agent.run_turn(sid, session, message)
        except Exception as exc:  # noqa: BLE001 - never a 500 for the caller; never a fake booking
            log.exception("turn failed: %s", exc)
            out = {"reply": "Sorry, something went wrong on my side. Nothing has been booked. Could you say that again?",
                   "state": "failed", "pending_action": agent._public(session.get("pending")), "result": None,
                   "tool_calls": [], "usage": None}
        _history(session, message, out["reply"])
        SESSIONS.save()
        if users.phone_of(sid):
            users.log_turn(users.phone_of(sid), message, out["reply"])
        return _response(sid, out, t0)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": MODEL, "mcp_tools": MCP.tools}


@app.post("/api/chat")
async def chat(body: ChatIn) -> dict:
    return await handle_chat(body.session_id, body.message)


@app.post("/api/approve")
async def approve(body: ApproveIn) -> dict:
    t0 = time.perf_counter()
    sid = body.session_id
    async with SESSIONS.locks[sid]:
        session = SESSIONS.get(sid)
        calls: list = []
        summary = (session.get("pending") or {}).get("summary") or ""
        if not session.get("clinic"):
            await agent.ensure_context(session, calls)
        out = await decide(session, body.action_id, body.approved, calls)
        if out.get("state"):
            session["state"] = out["state"]
        reply = out["reply"]  # written by code in the session's language: no LLM on the approval path
        pending = out.get("pending_action")
        resp = {"reply": reply, "state": out.get("state"), "pending_action": agent._public(pending),
                "result": out.get("result"), "tool_calls": calls, "usage": None}
        _history(session, "(Approved)" if body.approved else "(Rejected)", reply)
        SESSIONS.save()
        if users.phone_of(sid):
            users.log_turn(users.phone_of(sid), ("Approved: " if body.approved else "Rejected: ") + summary, reply)
        return _response(sid, resp, t0)


@app.post("/api/voice")
async def voice(audio: UploadFile = File(...), session_id: str = Form(...)) -> dict:
    from voice import STTError, speak, transcribe

    data = await audio.read()
    t_stt = time.perf_counter()
    try:
        text = await asyncio.to_thread(transcribe, data) if data else ""
    except STTError as exc:
        log.warning("STT failed: %s", exc)
        text = ""
    stt_ms = int((time.perf_counter() - t_stt) * 1000)
    response = await handle_chat(session_id, text)
    response["tool_calls"] = [{"tool": "speech_to_text", "ok": bool(text), "ms": stt_ms}] + response["tool_calls"]
    spoken = None
    try:
        lang = "ar" if any("؀" <= ch <= "ۿ" for ch in response["reply"]) else "en"
        spoken = await asyncio.to_thread(speak, response["reply"], lang)
    except Exception as exc:  # noqa: BLE001 - the reply is still shown, just not spoken
        log.warning("TTS failed: %s", exc)
    return {**response, "transcript": text,
            "audio_b64": base64.b64encode(spoken[0]).decode() if spoken else None,
            "audio_mime": spoken[1] if spoken else None}


@app.post("/api/speak")
async def speak_text(body: SpeakIn) -> dict:
    """Azure audio for a reply the UI got from /api/chat or /api/approve (not part of the eval contract)."""
    from voice import speak

    spoken = await asyncio.to_thread(speak, body.text)
    return {"audio_b64": base64.b64encode(spoken[0]).decode() if spoken else None,
            "audio_mime": spoken[1] if spoken else None}


# ---------------------------------------------------------------- sign-in, history, appointments (UI only)
async def _patient(phone: str) -> dict | None:
    """The clinic's record for this phone (with upcoming_bookings), or None for a new patient."""
    res = await MCP.call("get_patient", {"phone": phone}, [])
    return (res.get("patient") or res) if res.get("ok") else None


def _appointment_rows(patient: dict | None) -> list[dict]:
    return [{"date": b["date"], "weekday": b.get("weekday"), "start": b["start"], "doctor": b["provider_name"],
             "topic": b["service_name"], "booking_id": b["booking_id"]}
            for b in (patient or {}).get("upcoming_bookings") or [] if b.get("status", "confirmed") == "confirmed"]


async def login(text: str, phone_hint: str | None = None) -> dict:
    """Sign in with a phone number (and a name for a new patient). Never writes to the clinic."""
    phone = find_phone(text) or find_phone(phone_hint or "")
    if not phone:
        return {"ok": False, "message": "Please say or type your phone number, for example 0599000111."}
    patient = await _patient(phone)
    known = users.get(phone) or {}
    name = (patient or {}).get("name") or users.find_name(text, name_only=bool(phone_hint)) or known.get("name")
    if not name:
        return {"ok": False, "need_name": True, "phone": phone,
                "message": "I don't have this number yet. What is your name?"}
    users.upsert(phone, name)
    sid = users.session_id(phone)
    async with SESSIONS.locks[sid]:
        session = SESSIONS.get(sid)
        session["req"].update({"phone": phone, "patient_name": name})
        if patient:  # the agent knows the caller from the start (no "what's your phone number?")
            session.update({"patient": patient, "patient_phone": phone, "patient_greeted": False})
        SESSIONS.save()
    log.info("sign-in %s (%s patient)", phone, "returning" if patient else "new")
    return {"ok": True, "phone": phone, "name": name, "session_id": sid, "history": users.history(phone),
            "appointments": _appointment_rows(patient)}


@app.post("/api/login")
async def login_text(body: LoginIn) -> dict:
    return await login(body.text, body.phone)


@app.post("/api/login/voice")
async def login_voice(audio: UploadFile = File(...), phone: str = Form("")) -> dict:
    from voice import STTError, transcribe

    data = await audio.read()
    try:
        text = await asyncio.to_thread(transcribe, data) if data else ""
    except STTError as exc:
        log.warning("STT failed at sign-in: %s", exc)
        text = ""
    return {**await login(text, phone or None), "transcript": text}


@app.get("/api/history/{phone}")
def get_history(phone: str) -> dict:
    return {"phone": phone, "messages": users.history(phone)}


@app.get("/api/appointments/{phone}")
async def get_appointments(phone: str) -> dict:
    return {"phone": phone, "appointments": _appointment_rows(await _patient(phone))}

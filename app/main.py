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

from . import agent
from .approval import decide
from .config import MODEL, OPENROUTER_API_KEY, SESSIONS_FILE, log
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


class ChatIn(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=120)
    message: str = Field("", max_length=4000)
    user: dict[str, Any] | None = None


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
        if not session.get("clinic"):
            await agent.ensure_context(session, calls)
        out = await decide(session, body.action_id, body.approved, calls)
        if out.get("state"):
            session["state"] = out["state"]
        reply = out["reply"]
        if session.get("language") == "ar":
            turn = {"session": session, "draft": reply, "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
                    "allowed_minutes": out.get("allowed_minutes", []), "allowed_ids": out.get("allowed_ids", [])}
            reply = (await agent.speak_node(turn))["reply"]
        pending = out.get("pending_action")
        resp = {"reply": reply, "state": out.get("state"), "pending_action": agent._public(pending),
                "result": out.get("result"), "tool_calls": calls, "usage": None}
        _history(session, "(Approved)" if body.approved else "(Rejected)", reply)
        SESSIONS.save()
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
    except Exception as exc:  # noqa: BLE001 - the UI falls back to the browser voice
        log.warning("TTS failed: %s", exc)
    return {**response, "transcript": text,
            "audio_b64": base64.b64encode(spoken[0]).decode() if spoken else None,
            "audio_mime": spoken[1] if spoken else None}

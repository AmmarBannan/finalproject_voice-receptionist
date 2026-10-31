"""The receptionist as a LangGraph state machine.

    guard ─┬─ fixed reply (empty / emergency / medical advice)
           └─ context ─ intake (LLM) ─┬─ book    ─ patient ─ schedule ─┐
                                      ├─ cancel  ─ patient ─ cancel  ──┤
                                      ├─ confirm / reject ─ decision ──┤
                                      ├─ info    ─ info (LLM) ─────────┤
                                      └─ other ────────────────────────┴─ speak ─ END

Roles: the Intake agent (LLM, structured output) understands the caller; the Scheduler (code + MCP
read tools) checks the real calendar and prepares a pending_action; the Concierge (LLM) answers clinic
questions and speaks Arabic; the Approval step (approval.py) is the only code that writes.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from . import llm
from .approval import decide
from .config import log
from .guards import find_phone, input_guard, is_affirmative, norm_phone, output_check, strip_injection
from .mcp_client import MCP
from .prompts import CONCIERGE_SYSTEM, INFO_USER, INTAKE_SYSTEM, INTAKE_USER, REWRITE_USER
from .timeutil import WEEKDAYS, cap, time_mentions, norm_hhmm, resolve_date, spoken_day, spoken_time, to_min

SERVICES: dict[str, dict] = {}     # service_id -> service (cached from list_services)
PROVIDERS: dict[str, dict] = {}    # provider_id -> provider (from clinic_info)


class Intake(BaseModel):
    intent: str = "other"
    service_id: str | None = None
    date_ref: str | None = None
    exact_time: str | None = None
    after: str | None = None
    before: str | None = None
    provider_id: str | None = None
    choice_index: int | None = None
    patient_name: str | None = None
    caller_name: str | None = None
    guardian_name: str | None = None
    patient_age: int | None = None
    phone: str | None = None
    booking_id: str | None = None
    language: str | None = None
    injection_detected: bool = False


class Turn(TypedDict, total=False):
    session_id: str
    message: str
    session: dict
    guard: dict
    intake: dict
    route: str
    draft: str            # what to say (English, from code)
    reply: str
    speak_llm: bool       # rewrite the draft with the Concierge (Arabic)
    allowed_minutes: list
    allowed_ids: list
    state: str
    pending_action: dict | None
    result: dict | None
    tool_calls: list
    usage: dict


def new_session() -> dict:
    return {"history": [], "req": {}, "patient": None, "patient_phone": None, "clinic": None,
            "offered": [], "held_slot": None, "pending": None, "done_actions": {}, "language": "en",
            "state": "start"}


# ---------------------------------------------------------------- helpers
def _today(session: dict) -> date:
    return date.fromisoformat(session["clinic"]["today"])


def _now(session: dict) -> datetime:
    return datetime.fromisoformat(session["clinic"]["now"])


def _hours(session: dict, d: date) -> tuple[str, str] | None:
    h = (session["clinic"].get("opening_hours") or {}).get(WEEKDAYS[d.weekday()])
    return (h["open"], h["close"]) if h else None


def _svc_name(service_id: str) -> str:
    return (SERVICES.get(service_id) or {}).get("name_en", service_id.replace("_", " ")).lower().replace("gp ", "GP ")


def _slot_phrase(slot: dict, session: dict, with_day: bool = True, on: bool = False) -> str:
    d = date.fromisoformat(slot["date"])
    t = spoken_time(slot["start"])
    return f"{spoken_day(d, _today(session), on)} at {t} with {slot['provider_name']}" if with_day \
        else f"{t} with {slot['provider_name']}"


def _join(items: list[str], word: str = "or") -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" {word} " + items[-1]


def _allow(turn: Turn, minutes: list[int] = (), ids: list[str] = ()) -> None:
    turn.setdefault("allowed_minutes", []).extend(minutes)
    turn.setdefault("allowed_ids", []).extend(ids)


def _allow_slots(turn: Turn, slots: list[dict]) -> None:
    _allow(turn, [to_min(s["start"]) for s in slots])


def _allow_hours(turn: Turn, session: dict) -> None:
    for h in (session["clinic"].get("opening_hours") or {}).values():
        if h:
            _allow(turn, [to_min(h["open"]), to_min(h["close"])])


def _preferred(session: dict, service_id: str) -> str | None:
    req = session["req"]
    if req.get("provider_id"):
        return req["provider_id"]
    pref = (session.get("patient") or {}).get("preferred_provider_id")
    if pref and pref in (SERVICES.get(service_id) or {}).get("provider_ids", []):
        return pref
    return None


def _rank(slots: list[dict], preferred: str | None) -> list[dict]:
    return sorted(slots, key=lambda s: (s["provider_id"] != preferred, s["date"], s["start"]))


def _distinct_times(slots: list[dict], n: int) -> list[dict]:
    seen, out = set(), []
    for s in slots:
        key = (s["date"], s["start"])
        if key not in seen:
            seen.add(key)
            out.append(s)
        if len(out) == n:
            break
    return out


async def ensure_context(session: dict, calls: list) -> bool:
    """clinic_info once per session (today, hours, policies) and list_services once per process."""
    if not session.get("clinic"):
        info = await MCP.call("clinic_info", {}, calls)
        if not info.get("ok"):
            return False
        session["clinic"] = {k: info.get(k) for k in ("name", "phone", "address", "today", "weekday", "now",
                                                       "opening_hours", "policies", "emergency_message", "faq",
                                                       "last_bookable_day", "providers")}
        for p in info.get("providers") or []:
            PROVIDERS[p["id"]] = p
    if not SERVICES:
        res = await MCP.call("list_services", {}, calls)
        for s in res.get("services") or []:
            SERVICES[s["id"]] = s
    return True


# ---------------------------------------------------------------- nodes
async def guard_node(turn: Turn) -> Turn:
    msg, session = turn["message"], turn["session"]
    g = input_guard(msg)
    # The session keeps one language: set from the first real message, changed only when the caller asks
    # ("speak English", "بالعربي"). A phone number, a Latin-letter name or an "ok" never flips it.
    if g["language_request"]:
        session["language"], session["language_locked"] = g["language_request"], True
    elif not session.get("language_locked") and g["has_words"]:
        session["language"], session["language_locked"] = ("ar" if g["arabic"] else "en"), True
    ar = session["language"] == "ar"
    if g["empty"]:
        return {"guard": g, "route": "fixed", "state": session.get("state") or "start",
                "reply": "عذراً، لم أسمعك جيداً. هل يمكنك إعادة ذلك من فضلك؟" if ar
                else "Sorry, I didn't catch that. Could you say it again, please?"}
    if g["too_long"]:
        msg = msg[:2000]
    if g["injection"]:
        msg = strip_injection(msg)
        log.warning("injection attempt in session %s", turn["session_id"])
    if g["emergency"]:
        return {"guard": g, "route": "fixed", "state": "out_of_scope",
                "reply": ("إذا كانت هذه حالة طارئة، اتصل بالرقم 101 أو توجّه إلى أقرب قسم طوارئ الآن. "
                          "لا أستطيع تقديم نصيحة طبية عبر الهاتف.") if ar else
                         ("If this is an emergency, call 101 or go to the nearest emergency department now. "
                          "I'm the clinic's receptionist, so I can't assess symptoms or give medical advice.")}
    if g["medical_advice"]:
        return {"guard": g, "route": "fixed", "state": "out_of_scope",
                "reply": ("لا أستطيع تقديم نصيحة طبية أو وصف دواء، لكن يمكنني حجز موعد لك مع أحد أطبائنا. "
                          "هل تريد أن أبحث لك عن موعد؟") if ar else
                         ("I'm sorry, I can't give medical advice or recommend any medicine, but I can book you "
                          "a visit with one of our clinicians. If it gets worse or feels urgent, call 101. "
                          "Would you like me to find you an appointment?")}
    return {"guard": g, "message": msg, "route": "context"}


async def context_node(turn: Turn) -> Turn:
    if not await ensure_context(turn["session"], turn["tool_calls"]):
        return {"route": "fixed", "state": "failed",
                "reply": "عذراً، نظام الحجز لا يستجيب الآن، لذلك لا أستطيع الاطلاع على المواعيد. "
                         "يرجى المحاولة بعد بضع دقائق أو الاتصال بالعيادة مباشرة."
                if turn["session"]["language"] == "ar" else
                         "Sorry, our booking system is not answering right now, so I can't check the calendar. "
                         "Please try again in a few minutes, or call the clinic directly."}
    return {"route": "intake"}


async def intake_node(turn: Turn) -> Turn:
    session, req = turn["session"], turn["session"]["req"]
    c = session["clinic"]
    services = "\n".join(f"- {s['id']}: {s['name_en']} / {s['name_ar']} ({s['duration_min']} min)"
                         for s in SERVICES.values())
    providers = "\n".join(f"- {p['id']}: {p['name']} ({p.get('role', '')})" for p in PROVIDERS.values())
    offered = [{"index": i + 1, "date": s["date"], "start": s["start"], "provider": s["provider_name"]}
               for i, s in enumerate(session["offered"])]
    history = "\n".join(f"{h['role']}: {h['content']}" for h in session["history"][-6:]) or "(none)"
    try:
        x = await llm.structured(
            INTAKE_SYSTEM.format(today=c["today"], weekday=c["weekday"], services=services, providers=providers),
            INTAKE_USER.format(known=json.dumps(req, ensure_ascii=False),
                               offered=json.dumps(offered, ensure_ascii=False),
                               pending=(session["pending"] or {}).get("summary") or "none",
                               history=history, message=turn["message"]),
            Intake, turn["usage"], "intake")
    except Exception as exc:  # noqa: BLE001
        log.error("intake failed: %s", exc)
        return {"route": "fixed", "state": session["state"],
                "reply": "عذراً، أواجه صعوبة في الفهم الآن. هل يمكنك إعادة ذلك؟" if session["language"] == "ar"
                else "Sorry, I'm having trouble understanding right now. Could you say that again?"}
    if turn["guard"].get("injection"):
        x.injection_detected = True
    _merge(session, x, turn["message"])
    intent = x.intent if x.intent in {"book", "confirm", "reject", "cancel", "info", "medical", "emergency",
                                      "greeting", "other"} else "other"
    if intent == "confirm" and session["pending"] and not is_affirmative(turn["message"]):
        intent = "book"  # a restated request is not a "yes": never write on an ambiguous message
    if intent in ("confirm", "reject") and not session["pending"]:
        if intent == "confirm":
            intent = "book" if (session["held_slot"] or req.get("service_id")) else "other"
    log.info("intake intent=%s req=%s", intent, json.dumps(req, ensure_ascii=False))
    return {"intake": {**x.model_dump(), "intent": intent}, "route": intent}


def _merge(session: dict, x: Intake, message: str) -> None:
    """Fold this turn's extraction into the conversation's request (short-term memory)."""
    req = session["req"]
    if x.intent == "cancel":
        req["mode"] = "cancel"
    elif x.intent == "book":
        req["mode"] = "book"
    if x.service_id in SERVICES and x.service_id != req.get("service_id"):
        req["service_id"] = x.service_id
        session["offered"], session["held_slot"] = [], None
    if x.date_ref:
        d = "earliest" if x.date_ref.lower() in ("earliest", "asap") else resolve_date(x.date_ref, _today(session))
        d = d if isinstance(d, str) else (d.isoformat() if d else None)
        if d and d != req.get("date"):
            req["date"] = d
            session["held_slot"] = None
    times = {k: norm_hhmm(getattr(x, k)) for k in ("exact_time", "after", "before")}
    if any(times.values()):
        req.update(times)
        session["held_slot"] = None
    if x.provider_id in PROVIDERS:
        req["provider_id"] = x.provider_id
    phone = norm_phone(x.phone) or find_phone(message)
    if phone:
        req["phone"] = phone
    for k in ("patient_name", "guardian_name", "booking_id"):
        if getattr(x, k):
            req[k] = getattr(x, k).strip()
    if x.caller_name and x.patient_name and x.caller_name != x.patient_name and not req.get("guardian_name"):
        req["caller_name"] = x.caller_name
    if x.patient_age is not None:
        req["age"] = x.patient_age
    if x.choice_index and 1 <= x.choice_index <= len(session["offered"]):
        s = session["offered"][x.choice_index - 1]
        req.update({"date": s["date"], "exact_time": s["start"], "after": None, "before": None})
        req["choice_slot"] = s["slot_id"]


async def patient_node(turn: Turn) -> Turn:
    """Persistent memory: recognise a returning patient by phone."""
    session, req = turn["session"], turn["session"]["req"]
    phone = req.get("phone")
    if phone and phone != session.get("patient_phone"):
        res = await MCP.call("get_patient", {"phone": phone}, turn["tool_calls"])
        session["patient_phone"] = phone
        if res.get("ok"):
            p = res.get("patient") or res
            session["patient"] = p
            session["patient_greeted"] = False
            if not req.get("patient_name") or _same_person(req["patient_name"], p.get("name", "")):
                req["patient_name"] = p.get("name")  # the record's spelling beats the transcript's
        else:
            session["patient"] = None
            if (res.get("error") or {}).get("code") not in ("unknown_patient", "invalid_phone"):
                session["patient_phone"] = None  # lookup failed (outage): try again next turn
    return {}


def _same_person(a: str, b: str) -> bool:
    from difflib import SequenceMatcher
    return any(SequenceMatcher(None, x, y).ratio() >= 0.75
               for x in a.lower().split() for y in b.lower().split())


def _greeting(session: dict) -> str:
    p = session.get("patient")
    if p and not session.get("patient_greeted"):
        session["patient_greeted"] = True
        return f"Welcome back, {p.get('name', '').split()[0]}. "
    return ""


def _make_pending(session: dict, slot: dict) -> dict:
    req = session["req"]
    svc = slot.get("service_id") or req["service_id"]
    payload = {"slot_id": slot["slot_id"], "service_id": svc, "patient_name": req["patient_name"],
               "patient_phone": req["phone"]}
    who = f"{req['patient_name']} ({req['phone']})"
    if req.get("guardian_name"):
        payload["guardian_name"] = req["guardian_name"]
        who += f", guardian {req['guardian_name']}"
    d = date.fromisoformat(slot["date"])
    summary = (f"Book: {cap(_svc_name(svc))} · {d.strftime('%a %b')} {d.day} at {spoken_time(slot['start'])} "
               f"· {slot['provider_name']} · for {who}")
    return {"action_id": "act-" + uuid.uuid4().hex[:10], "type": "create_booking", "summary": summary,
            "payload": payload, "slot": slot}


def _missing_detail(session: dict) -> str | None:
    req = session["req"]
    if not req.get("patient_name") and not req.get("phone"):
        return "May I have your full name and phone number?"
    if not req.get("phone"):
        return "And what's the best phone number for you?"
    if not req.get("patient_name"):
        return "And may I have the patient's full name?"
    if req.get("service_id") == "pediatrics_visit" or (req.get("age") is not None and req["age"] < 16):
        if not req.get("guardian_name"):
            if req.get("caller_name"):
                req["guardian_name"] = req["caller_name"]
            else:
                return "Since the patient is under 16, may I have the parent or guardian's full name?"
    return None


async def schedule_node(turn: Turn) -> Turn:
    """The Scheduler: real slots only, nearest alternatives, read-back as a pending_action."""
    session, req, calls = turn["session"], turn["session"]["req"], turn["tool_calls"]
    session["pending"] = None  # a new request replaces an old read-back: one pending action at a time
    hello = _greeting(session) + _injection_notice(turn)
    sid = req.get("service_id")
    if not sid:
        return {"draft": hello + "What kind of appointment do you need? We offer dental check-ups, cleanings and "
                                 "fillings, GP consultations, pediatrics visits and physiotherapy.",
                "state": "collect_details"}
    if not req.get("date"):
        return {"draft": hello + f"Which day would suit you for the {_svc_name(sid)}?", "state": "collect_details"}

    today = _today(session)
    preferred = _preferred(session, sid)
    chosen: dict | None = None
    exact = req.get("exact_time")

    # the patient already picked a slot we found earlier; we only needed their details
    held = session.get("held_slot")
    if held and held.get("service_id") == sid:
        chosen = held

    if not chosen and req.get("date") == "earliest":
        d_from, d_to = today, today + timedelta(days=6)
    elif not chosen:
        d_from = d_to = date.fromisoformat(req["date"])
    if not chosen:
        after, before = req.get("after"), req.get("before")
        if exact:  # look 3 hours either side, so the nearest alternatives come from the same call
            after = f"{max(0, to_min(exact) - 180) // 60:02d}:{max(0, to_min(exact) - 180) % 60:02d}"
            before = f"{min(1439, to_min(exact) + 181) // 60:02d}:{min(1439, to_min(exact) + 181) % 60:02d}"
        args = {"service_id": sid, "date_from": d_from.isoformat(), "date_to": d_to.isoformat(), "limit": 50,
                "after": after, "before": before, "provider_id": req.get("provider_id")}
        res = await MCP.call("find_slots", args, calls)
        if not res.get("ok"):
            return _availability_error(session, res)
        slots, notes = res.get("slots") or [], res.get("notes") or []
        day = d_from

        if exact:
            at = [s for s in slots if s["start"] == exact]
            if at:
                chosen = _rank(at, preferred)[0]
            else:
                return await _offer_alternatives(turn, sid, day, slots, exact=exact, notes=notes, hello=hello)
        elif slots:
            ranked = _rank(slots, preferred)
            if preferred and ranked[0]["provider_id"] != preferred:
                lead = f"{PROVIDERS.get(preferred, {}).get('name', 'Your usual provider')} has no free time then, but "
            else:
                lead = ""
            offer = _distinct_times(ranked, 3)
            session["offered"] = offer
            _allow_slots(turn, offer)
            if len(offer) == 1:
                chosen = offer[0]
                session["offered"] = offer
            else:
                window = _window_words(req)
                same_day = len({s["date"] for s in offer}) == 1
                head = f"{cap(spoken_day(date.fromisoformat(offer[0]['date']), today))}{window}: " if same_day else ""
                opts = _join([_slot_phrase(s, session, with_day=not same_day) for s in offer])
                ask = " Which one suits you?"
                missing = _missing_detail(session)
                if missing:
                    ask += " " + missing
                return {"draft": f"{hello}{head}{lead}I have {opts}.{ask}", "state": "propose_slots"}
        else:
            return await _offer_alternatives(turn, sid, day, [], exact=None, notes=notes, hello=hello)

    # we have one concrete slot: ask for what's missing, or read it back and wait for approval
    chosen = {**chosen, "service_id": sid}
    _allow_slots(turn, [chosen])
    missing = _missing_detail(session)
    if missing:
        session["held_slot"] = chosen
        return {"draft": f"{hello}{cap(_slot_phrase(chosen, session))} is free for a {_svc_name(sid)}. {missing}",
                "state": "collect_details"}
    pending = _make_pending(session, chosen)
    session["pending"], session["held_slot"] = pending, None
    who = req["patient_name"] + (f" (guardian {req['guardian_name']})" if req.get("guardian_name") else "")
    draft = (f"{hello}To confirm: a {_svc_name(sid)} {_slot_phrase(chosen, session, on=True)}, for {who}. "
             f"Shall I book it? You can say yes or press Approve.")
    return {"draft": draft, "state": "await_confirmation", "pending_action": pending}


def _injection_notice(turn: Turn) -> str:
    if turn.get("guard", {}).get("injection") or turn.get("intake", {}).get("injection_detected"):
        return "I can only book one appointment at a time, and only after you confirm it. "
    return ""


def _window_words(req: dict) -> str:
    a, b = req.get("after"), req.get("before")
    if a and b:
        return f" between {spoken_time(a)} and {spoken_time(b)}"
    if a:
        return f" after {spoken_time(a)}"
    if b:
        return f" before {spoken_time(b)}"
    return ""


def _availability_error(session: dict, res: dict) -> Turn:
    err = res.get("error") or {}
    if err.get("code") == "policy_violation" or res.get("status") in (400, 422):
        last = session["clinic"].get("last_bookable_day")
        msg = f"I can only book up to 21 days ahead, until {last}." if last else ""
        return {"draft": f"Sorry, I can't search that date. {err.get('message') or msg} Which other day would suit you?",
                "state": "collect_details"}
    return {"draft": "Sorry, our booking system isn't answering right now, so I can't check the calendar. "
                     "Nothing has been booked. Please try again in a moment.", "state": "failed"}


def nearest(pool: list[dict], target: int, preferred: str | None) -> list[dict]:
    """The closest free slot before and the closest after `target` (minutes), preferred provider on ties."""
    before = sorted([s for s in pool if to_min(s["start"]) < target], key=lambda s: -to_min(s["start"]))
    after = sorted([s for s in pool if to_min(s["start"]) > target], key=lambda s: to_min(s["start"]))
    pick = []
    for group in (before, after):
        if group:
            same = [s for s in group if s["start"] == group[0]["start"]]
            pick.append(_rank(same, preferred)[0])
    return sorted(pick, key=lambda s: s["start"])


async def _offer_alternatives(turn: Turn, sid: str, day: date, day_slots: list[dict], *, exact: str | None,
                              notes: list[str], hello: str) -> Turn:
    """The requested time is taken, outside hours, or the day is closed: offer the nearest real slots."""
    session, req, calls = turn["session"], turn["session"]["req"], turn["tool_calls"]
    today = _today(session)
    preferred = _preferred(session, sid)
    hours = _hours(session, day)
    reasons = []
    target = to_min(exact) if exact else to_min(req.get("after") or req.get("before") or "12:00")
    if hours is None:
        reasons.append(f"The clinic is closed on {day.strftime('%A')}s.")
    else:
        _allow(turn, [to_min(hours[0]), to_min(hours[1])])
        dur = (SERVICES.get(sid) or {}).get("duration_min", 30)
        if exact and (to_min(exact) + dur > to_min(hours[1]) or to_min(exact) < to_min(hours[0])) or \
                (req.get("after") and to_min(req["after"]) + dur > to_min(hours[1])):
            reasons.append(f"On {day.strftime('%A')} the clinic is open {spoken_time(hours[0])} to "
                           f"{spoken_time(hours[1])}, so there is nothing at that time.")
        elif exact:
            _allow(turn, [to_min(exact)])
            reasons.append(f"Sorry, {spoken_time(exact)} on {day.strftime('%A')} is already taken.")
        else:
            reasons.append(f"Sorry, there's nothing free{_window_words(req)} on {day.strftime('%A')}.")

    # nearest on the same day (any time), then the next days in the same window, then any time
    pool = day_slots
    if not pool and hours is not None:
        res = await MCP.call("find_slots", {"service_id": sid, "date_from": day.isoformat(), "date_to": day.isoformat(),
                                            "limit": 50, "provider_id": req.get("provider_id")}, calls)
        pool = res.get("slots") or [] if res.get("ok") else []
    if pool:
        offer = nearest(pool, target, preferred)
    else:
        last = date.fromisoformat(session["clinic"].get("last_bookable_day") or (today + timedelta(days=21)).isoformat())
        d1, d2 = max(day + timedelta(days=1), today), min(day + timedelta(days=7), last)
        offer = []
        if d1 <= d2:
            for window in ((req.get("after"), req.get("before")), (None, None)):
                res = await MCP.call("find_slots", {"service_id": sid, "date_from": d1.isoformat(),
                                                    "date_to": d2.isoformat(), "after": window[0], "before": window[1],
                                                    "limit": 20, "provider_id": req.get("provider_id")}, calls)
                if res.get("ok") and res.get("slots"):
                    offer = _distinct_times(_rank(res["slots"], preferred), 2) if not preferred else \
                        _distinct_times(sorted(res["slots"], key=lambda s: (s["date"], s["provider_id"] != preferred, s["start"])), 2)
                    break
    session["offered"], session["held_slot"] = offer, None
    _allow_slots(turn, offer)
    if not offer:
        return {"draft": hello + " ".join(reasons) + " I couldn't find a free time nearby. Would another day work for you?",
                "state": "alternatives"}
    same_day = all(s["date"] == day.isoformat() for s in offer)
    opts = _join([_slot_phrase(s, session, with_day=not same_day) for s in offer])
    plural = len(offer) > 1
    lead = ("The nearest free times that day are" if plural else "The nearest free time that day is") if same_day         else ("The nearest free times are" if plural else "The nearest free time is")
    ask = " Which one would you like?"
    missing = _missing_detail(session)
    if missing:
        ask += " " + missing
    return {"draft": f"{hello}{' '.join(reasons)} {lead} {opts}.{ask}", "state": "alternatives"}


async def cancel_node(turn: Turn) -> Turn:
    session, req, calls = turn["session"], turn["session"]["req"], turn["tool_calls"]
    session["pending"] = None
    if not req.get("phone"):
        return {"draft": "I can help with that. What's the phone number the appointment was booked under?",
                "state": "collect_details"}
    p = session.get("patient")
    if not p:
        return {"draft": "I couldn't find any appointments under that phone number. Could you check the number?",
                "state": "collect_details"}
    upcoming = p.get("upcoming_bookings") or []
    _allow(turn, ids=[b["booking_id"] for b in upcoming])
    match = upcoming
    if req.get("booking_id"):
        match = [b for b in upcoming if b["booking_id"].upper() == req["booking_id"].upper()]
        if not match:
            return {"draft": f"I can't find booking {req['booking_id'].upper()} under this phone number. "
                             "Could you check the reference?", "state": "collect_details",
                    "allowed_ids": [req["booking_id"].upper()]}
    else:
        if req.get("service_id"):
            match = [b for b in match if b.get("service_id") == req["service_id"]] or match
        if req.get("date") and req["date"] != "earliest":
            match = [b for b in match if b.get("date") == req["date"]] or match
    if not match:
        return {"draft": "You don't have any upcoming appointments with us, so there's nothing to cancel.",
                "state": "completed"}

    def phrase(b: dict) -> str:
        d = date.fromisoformat(b["date"])
        return f"the {_svc_name(b.get('service_id', ''))} {spoken_day(d, _today(session), True)} at {spoken_time(b['start'])} with {b.get('provider_name')}"

    for b in match:
        _allow(turn, [to_min(b["start"])])
    if len(match) > 1:
        return {"draft": f"You have {len(match)} upcoming appointments: {_join([phrase(b) for b in match], 'and')}. "
                         "Which one would you like to cancel?", "state": "collect_details"}
    b = match[0]
    start = datetime.fromisoformat(f"{b['date']}T{b['start']}").replace(tzinfo=_now(session).tzinfo)
    min_h = (session["clinic"].get("policies") or {}).get("cancellation_min_hours", 24)
    if start - _now(session) < timedelta(hours=min_h):
        return {"draft": f"{cap(phrase(b))} starts in less than {min_h} hours, so I can't cancel it here. "
                         f"Please call the clinic on {session['clinic'].get('phone')}.", "state": "rejected"}
    d = date.fromisoformat(b["date"])
    pending = {"action_id": "act-" + uuid.uuid4().hex[:10], "type": "cancel_booking",
               "summary": f"Cancel {b['booking_id']}: {b.get('service_name')} · {d.strftime('%a %b')} {d.day} at "
                          f"{spoken_time(b['start'])} · {b.get('provider_name')} · for {b.get('patient_name') or p.get('name')}",
               "payload": {"booking_id": b["booking_id"], "reason": "Cancelled by the patient by phone"},
               "booking": {k: b.get(k) for k in ("booking_id", "service_name", "date", "start", "provider_name")}}
    session["pending"] = pending
    return {"draft": f"To confirm: you want to cancel {phrase(b)}, reference {b['booking_id']}. Shall I cancel it?",
            "state": "await_confirmation", "pending_action": pending}


async def decision_node(turn: Turn) -> Turn:
    """'Yes' / 'no' said in the conversation counts like the Approve / Reject buttons."""
    session = turn["session"]
    pending = session["pending"]
    out = await decide(session, pending["action_id"], turn["intake"]["intent"] == "confirm", turn["tool_calls"])
    # "reply", not "draft": decide() already wrote it in the session's language, so speak_node keeps it as is
    return {"reply": out["reply"], "state": out["state"], "pending_action": out.get("pending_action"),
            "result": out.get("result"), "allowed_minutes": out.get("allowed_minutes", []),
            "allowed_ids": out.get("allowed_ids", [])}


async def info_node(turn: Turn) -> Turn:
    session = turn["session"]
    c = session["clinic"]
    facts = {k: c.get(k) for k in ("name", "address", "phone", "opening_hours", "policies", "emergency_message", "faq")}
    facts["providers"] = [{"name": p["name"], "role": p.get("role"), "working_days": p.get("working_days"),
                           "languages": p.get("languages")} for p in PROVIDERS.values()]
    facts["services"] = [{"name": s["name_en"], "name_ar": s["name_ar"], "minutes": s["duration_min"],
                          "price_ils": s.get("price_ils")} for s in SERVICES.values()]
    facts["today"] = f"{c['today']} ({c['weekday']})"
    _allow_hours(turn, session)
    lang = "Arabic" if session["language"] == "ar" else "English"
    try:
        reply = await llm.text(CONCIERGE_SYSTEM.format(language=lang),
                               INFO_USER.format(facts=json.dumps(facts, ensure_ascii=False), question=turn["message"],
                                                language=lang), turn["usage"], "concierge-info")
    except Exception:  # noqa: BLE001
        reply = ""
    problems = output_check(reply, allowed_minutes=set(turn.get("allowed_minutes", [])), allowed_ids=set()) if reply else ["empty"]
    if problems:
        log.warning("info reply rejected: %s", problems)
        hrs = "; ".join(f"{d.capitalize()} {h['open']}-{h['close']}" if h else f"{d.capitalize()} closed"
                        for d, h in (c.get("opening_hours") or {}).items())
        reply = f"Our opening hours are: {hrs}. How else can I help?"
    return {"reply": reply, "state": "info"}


async def other_node(turn: Turn) -> Turn:
    intent = turn.get("intake", {}).get("intent")
    if intent in ("medical", "emergency"):
        return await guard_like(turn, intent)
    hello = _greeting(turn["session"])
    if turn.get("guard", {}).get("language_request") and intent in ("greeting", "other"):
        # a bare "please speak Arabic": acknowledge it (speak_node says it in the new language)
        return {"draft": hello + "Of course. How can I help you today?", "state": turn["session"].get("state") or "start"}
    if intent == "reject":
        return {"draft": "No problem. Is there anything else I can help you with?", "state": "completed"}
    if intent == "greeting":
        return {"draft": hello + "Hello, this is Zaytoun Family Clinic. I can book, check or cancel an appointment "
                                 "for you. How can I help?", "state": "start"}
    return {"draft": hello + "I'm the clinic's receptionist, so I can help with booking, checking or cancelling "
                             "appointments, and questions about the clinic. What would you like to do?",
            "state": "out_of_scope"}


async def guard_like(turn: Turn, intent: str) -> Turn:
    """The Intake agent spotted a medical/emergency message the regex guard missed."""
    ar = turn["session"]["language"] == "ar"
    if intent == "emergency":
        return {"reply": "إذا كانت هذه حالة طارئة، اتصل بالرقم 101 أو توجّه إلى أقرب قسم طوارئ الآن." if ar else
                "If this is an emergency, call 101 or go to the nearest emergency department now. "
                "I can't assess symptoms or give medical advice.", "state": "out_of_scope"}
    return {"reply": "لا أستطيع تقديم نصيحة طبية، لكن يمكنني حجز موعد لك مع أحد أطبائنا. هل تريد ذلك؟" if ar else
            "I'm sorry, I can't give medical advice, but I can book you a visit with one of our clinicians. "
            "Would you like me to find you an appointment?", "state": "out_of_scope"}


async def speak_node(turn: Turn) -> Turn:
    """Final reply: the code-written draft, or the Concierge's Arabic version if it passes the output check."""
    session = turn["session"]
    reply = turn.get("reply")
    if not reply:
        draft = turn.get("draft") or "How can I help you?"
        reply = draft
        if session["language"] == "ar":
            try:
                ar = await llm.text(CONCIERGE_SYSTEM.format(language="Arabic"),
                                    REWRITE_USER.format(language="Arabic", draft=draft), turn["usage"], "concierge-ar")
                # the draft was written by code from tool results, so every time in it is allowed
                allowed = set(turn.get("allowed_minutes", [])).union(*time_mentions(draft))
                ids = {i.upper() for i in turn.get("allowed_ids", [])}
                problems = output_check(ar, allowed_minutes=allowed, allowed_ids=ids)
                if problems:
                    log.warning("arabic reply rejected: %s", problems)
                else:
                    reply = ar
            except Exception as exc:  # noqa: BLE001 - keep the English draft
                log.warning("concierge failed: %s", exc)
    # last line of defence for code drafts too: a BK- id must come from the system
    problems = output_check(reply, allowed_minutes=set(range(0, 1440)),
                            allowed_ids={i.upper() for i in turn.get("allowed_ids", [])})
    if any("booking id" in p for p in problems):
        log.error("blocked reply with an unverified booking id: %s", reply)
        reply = "Sorry, something went wrong on my side and nothing was booked. Could we try again?"
    return {"reply": reply}


# ---------------------------------------------------------------- graph
def _route(turn: Turn) -> str:
    return turn["route"]


def build_graph():
    g = StateGraph(Turn)
    for name, fn in [("guard", guard_node), ("context", context_node), ("intake", intake_node),
                     ("patient_book", patient_node), ("patient_cancel", patient_node), ("schedule", schedule_node),
                     ("cancel", cancel_node), ("decision", decision_node), ("info", info_node),
                     ("other", other_node), ("speak", speak_node)]:
        g.add_node(name, fn)
    g.set_entry_point("guard")
    g.add_conditional_edges("guard", _route, {"fixed": "speak", "context": "context"})
    g.add_conditional_edges("context", _route, {"fixed": "speak", "intake": "intake"})
    g.add_conditional_edges("intake", _route, {
        "fixed": "speak", "book": "patient_book", "cancel": "patient_cancel", "confirm": "decision",
        "reject": "decision_or_other", "info": "info", "medical": "other", "emergency": "other",
        "greeting": "other", "other": "other"})
    g.add_node("decision_or_other", lambda t: {})
    g.add_conditional_edges("decision_or_other", lambda t: "decision" if t["session"]["pending"] else "other",
                            {"decision": "decision", "other": "other"})
    g.add_edge("patient_book", "schedule")
    g.add_edge("patient_cancel", "cancel")
    for n in ("schedule", "cancel", "decision", "info", "other"):
        g.add_edge(n, "speak")
    g.add_edge("speak", END)
    return g.compile()


GRAPH = build_graph()


async def run_turn(session_id: str, session: dict, message: str) -> dict[str, Any]:
    calls: list = []
    usage = llm.new_usage()
    out = await GRAPH.ainvoke({"session_id": session_id, "message": message, "session": session,
                               "tool_calls": calls, "usage": usage, "allowed_minutes": [], "allowed_ids": []})
    session["state"] = out.get("state") or session.get("state")
    pending = session.get("pending")
    return {"reply": out["reply"], "state": session["state"], "pending_action": _public(pending),
            "result": out.get("result"), "tool_calls": calls, "usage": usage}


def _public(pending: dict | None) -> dict | None:
    if not pending:
        return None
    return {k: pending[k] for k in ("action_id", "type", "summary", "payload")}

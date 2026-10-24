"""The approval gate: the ONLY code path that writes to the clinic system.

The LLM proposes (a pending_action stored in the session); this module commits it through MCP with
exactly the stored payload and Idempotency-Key = action_id, so a double click cannot double-book.
"""
from __future__ import annotations

from datetime import date

from .config import log
from .mcp_client import MCP
from .timeutil import spoken_time, to_min


def _shift(hhmm: str, minutes: int) -> str:
    m = min(1439, max(0, to_min(hhmm) + minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


async def decide(session: dict, action_id: str, approved: bool, calls: list) -> dict:
    """Returns {reply, state, result?, pending_action?, allowed_minutes, allowed_ids}."""
    done = session["done_actions"].get(action_id)
    if done:  # the double click: answer again, write nothing again
        log.info("action %s already executed, replaying", action_id)
        return {**done, "reply": "That's already done. " + done["reply"]}
    pending = session.get("pending")
    if not pending or pending["action_id"] != action_id:
        return {"reply": "I don't have that request waiting for your approval, so nothing was changed. "
                         "Would you like me to look for a time?", "state": session.get("state") or "start",
                "pending_action": pending}

    if not approved:
        session["pending"] = None
        session["held_slot"] = None
        session["req"].pop("exact_time", None)
        session["req"].pop("choice_slot", None)
        what = "booked" if pending["type"] == "create_booking" else "cancelled"
        return {"reply": f"No problem, nothing has been {what}. Would you like a different time or day instead?",
                "state": "rejected"}

    if pending["type"] == "create_booking":
        return await _book(session, pending, calls)
    if pending["type"] == "cancel_booking":
        return await _cancel(session, pending, calls)
    session["pending"] = None
    return {"reply": "Sorry, I can't do that kind of action.", "state": "failed"}


async def _book(session: dict, pending: dict, calls: list) -> dict:
    p, slot = pending["payload"], pending["slot"]
    res = await MCP.call_write("create_booking", {**p, "idempotency_key": pending["action_id"]}, calls)
    if res.get("ok") and str(res.get("booking_id", "")).startswith("BK-"):
        b = res
        bid = b["booking_id"]
        session["pending"] = None
        reply = (f"Booked. Your reference is {bid}: a {b.get('service_name', '').lower() or 'visit'} on "
                 f"{_day(b['date'])} at {spoken_time(b['start'])} with {b.get('provider_name')}.")
        if b.get("new_patient"):
            reply += " As a new patient, please arrive 10 minutes early."
        reply += " See you then!"
        out = {"reply": reply, "state": "completed",
               "result": {"booking_id": bid, "slot_id": b.get("slot_id"), "date": b.get("date"),
                          "start": b.get("start"), "provider_name": b.get("provider_name")},
               "allowed_minutes": [to_min(b["start"])], "allowed_ids": [bid]}
        session["done_actions"][pending["action_id"]] = out
        await _remember(session, p, b, calls)
        # the request is fulfilled: keep who the caller is, forget the booking details
        req = session["req"]
        session["req"] = {k: req[k] for k in ("patient_name", "phone") if req.get(k)}
        session["offered"] = []
        return out

    err = res.get("error") or {}
    code, status = err.get("code"), res.get("status")
    if code == "slot_taken" or status == 409:
        session["pending"] = None
        alt = await MCP.call("find_slots", {"service_id": p["service_id"], "date_from": slot["date"],
                                            "date_to": slot["date"], "limit": 50,
                                            "after": _shift(slot["start"], -180), "before": _shift(slot["start"], 181)},
                            calls)
        from .agent import nearest  # local import: agent imports this module
        near = nearest([s for s in alt.get("slots") or [] if s["slot_id"] != slot["slot_id"]],
                       to_min(slot["start"]), slot.get("provider_id"))
        session["offered"] = near
        if near:
            opts = " or ".join(f"{spoken_time(s['start'])} with {s['provider_name']}" for s in near)
            reply = (f"I'm sorry, {spoken_time(slot['start'])} was just taken by someone else, so nothing was booked. "
                     f"On {_day(slot['date'])} I can still offer {opts}. Which would you like?")
        else:
            reply = (f"I'm sorry, {spoken_time(slot['start'])} was just taken by someone else, so nothing was booked. "
                     "Would you like me to look at another day?")
        return {"reply": reply, "state": "alternatives",
                "allowed_minutes": [to_min(slot["start"])] + [to_min(s["start"]) for s in near]}
    if code == "policy_violation" or status in (400, 422):
        session["pending"] = None
        return {"reply": f"Sorry, I couldn't book that: {err.get('message', 'it breaks a clinic policy')}. "
                         "Would you like to try something else?", "state": "failed"}
    # 5xx, timeout, unreachable: nothing was written. Keep the pending action so the caller can retry
    # with the same idempotency key.
    return {"reply": "Sorry, our booking system is unavailable right now, so your appointment was NOT booked. "
                     "Would you like me to try again in a moment, or choose another time?",
            "state": "failed", "pending_action": pending}


async def _cancel(session: dict, pending: dict, calls: list) -> dict:
    p = pending["payload"]
    res = await MCP.call_write("cancel_booking", p, calls)
    bid = p["booking_id"]
    if res.get("ok"):
        session["pending"] = None
        out = {"reply": f"Done: booking {bid} is cancelled. Would you like to book another time?",
               "state": "completed", "result": {"booking_id": bid, "status": "cancelled"}, "allowed_ids": [bid]}
        session["done_actions"][pending["action_id"]] = out
        session["patient_phone"] = None  # re-read the record next time: its upcoming list changed
        return out
    err = res.get("error") or {}
    if err.get("code") == "too_late_to_cancel":
        session["pending"] = None
        return {"reply": "Sorry, that appointment starts in less than 24 hours, so I can't cancel it here. "
                         f"Please call the clinic on {session['clinic'].get('phone')}.", "state": "failed",
                "allowed_ids": [bid]}
    if err.get("code") == "already_cancelled":
        session["pending"] = None
        return {"reply": f"Booking {bid} was already cancelled.", "state": "completed", "allowed_ids": [bid]}
    return {"reply": "Sorry, our booking system is unavailable right now, so the appointment was NOT cancelled. "
                     "Would you like me to try again in a moment?", "state": "failed", "pending_action": pending,
            "allowed_ids": [bid]}


async def _remember(session: dict, payload: dict, booking: dict, calls: list) -> None:
    """Persistent memory: save the patient's language and (if they had none) their provider."""
    patient = session.get("patient") or {}
    args = {"phone": payload["patient_phone"], "name": patient.get("name") or payload["patient_name"],
            "language": session.get("language") or "en"}
    if not patient.get("preferred_provider_id"):
        args["preferred_provider_id"] = booking.get("provider_id")
    if payload.get("guardian_name") and patient.get("name"):
        return  # a child booked on a guardian's phone: keep the guardian's record as it is
    res = await MCP.call("save_patient", args, calls)
    if res.get("ok"):
        session["patient"] = res.get("patient") or {**patient, **args}

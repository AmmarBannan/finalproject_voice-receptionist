"""Gradio client for the receptionist API contract: POST /api/chat, /api/voice and /api/approve.

Start your app first (APP_URL, default http://localhost:8000), then:
    python ui/app_ui.py        ->  http://localhost:7860

Sign in first with your phone number (typed or spoken; new patients add their name). Your name shows at
the top, your past messages load and can be viewed by date, and your upcoming appointments are listed at
the bottom right. Speak (record or upload) or type. When your app proposes a booking (pending_action), Approve or
Reject it. Replies are spoken with Azure: /api/voice returns audio_b64; for typed and Approve/Reject
replies the UI asks POST /api/speak.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import gradio as gr
import httpx

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

APP_URL = (os.getenv("APP_URL") or "http://localhost:8000").rstrip("/")
TIMEOUT = float(os.getenv("UI_TIMEOUT") or 120)

# Runs in the browser after every reply: play the app's Azure audio (stopping the previous reply first).
SPEAK_JS = """(speech) => {
  if (!speech) return;
  const s = JSON.parse(speech);
  if (window._replyAudio) window._replyAudio.pause();
  if (!s.audio) return;
  window._replyAudio = new Audio(s.audio);
  window._replyAudio.play().catch((e) => console.warn("autoplay blocked", e));
}"""

# Runs in the browser when Send is pressed: if the mic is still recording, click its Stop button and return
# true, so the recording is sent by the mic's stop_recording event once the clip is ready.
STOP_RECORDING_JS = """() => {
  const stop = [...document.querySelectorAll("#mic button.stop-button, #mic button.stop-button-paused")]
    .find((b) => b.getClientRects().length);
  if (!stop) return false;
  setTimeout(() => stop.click(), 100);  // after the returned flag has been applied
  return true;
}"""


def post(path: str, **kwargs) -> dict:
    try:
        r = httpx.post(APP_URL + path, timeout=TIMEOUT, **kwargs)
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"reply": f"Cannot reach your app at {APP_URL}{path} ({exc.__class__.__name__})."}
    if r.status_code >= 400 or not isinstance(data, dict):
        return {"reply": f"Your app answered HTTP {r.status_code}: {json.dumps(data)[:300]}"}
    return data


def get(path: str) -> dict:
    try:
        r = httpx.get(APP_URL + path, timeout=TIMEOUT)
        return r.json() if r.status_code < 400 else {}
    except Exception:  # noqa: BLE001
        return {}


# browser tab (Gradio session_hash) -> signed-in user {"phone", "name", "session_id"}
LOGINS: dict[str, dict] = {}
APPT_HEADERS = ["Date", "Time", "Doctor", "Appointment"]


def session_id(request: gr.Request) -> str:
    user = LOGINS.get(request.session_hash or "local")
    return user["session_id"] if user else "ui-" + (request.session_hash or "local")


def _day_choices(messages: list) -> list[str]:
    """Dates with messages, newest first; today is always there so a new conversation has a home."""
    days = {m["ts"][:10] for m in messages if m.get("ts")} | {date.today().isoformat()}
    return sorted(days, reverse=True)


def _day_chat(messages: list, day: str) -> list[dict]:
    return [{"role": m["role"], "content": m["content"]} for m in messages if (m.get("ts") or "")[:10] == day]


def _appt_rows(appointments: list) -> list[list]:
    return [[f"{(a.get('weekday') or '')[:3]} {a['date']}".strip(), a["start"], a["doctor"], a["topic"]]
            for a in appointments] or [["-", "-", "No upcoming appointments", "-"]]


def show(data: dict, history: list) -> tuple:
    """Map one ChatResponse onto the UI components."""
    if not data.get("audio_b64") and data.get("reply"):  # typed / approve replies: fetch the Azure audio
        data = {**data, **{k: v for k, v in post("/api/speak", json={"text": data["reply"]}).items()
                           if k in ("audio_b64", "audio_mime")}}
    history = history + [{"role": "assistant", "content": data.get("reply") or "(empty reply)"}]
    pending = data.get("pending_action")
    calls = data.get("tool_calls") or []
    tools_md = "\n".join(f"- `{c.get('tool')}` {'ok' if c.get('ok') else '**failed**'} ({c.get('ms', '?')} ms)"
                         for c in calls) or "_none_"
    status = f"**State:** {data.get('state') or '-'}"
    if data.get("result"):
        status += f"  \n**Result:** `{json.dumps(data['result'], ensure_ascii=False)}`"
    if (data.get("usage") or {}).get("cost_usd") is not None:
        status += f"  \n**Cost of this turn:** ${float(data['usage']['cost_usd']):.4f}"
    pending_md = f"### Approve this?\n{pending.get('summary', '')}" if pending else ""
    audio = f"data:{data.get('audio_mime') or 'audio/wav'};base64,{data['audio_b64']}" if data.get("audio_b64") else None
    speech = json.dumps({"text": data.get("reply") or "", "audio": audio})
    return (history, data.get("transcript") or "", status, tools_md, pending_md, pending,
            gr.update(visible=bool(pending)), gr.update(visible=bool(pending)), speech)


def send_text(message: str, audio_path: str | None, history: list, request: gr.Request):
    """Send button / Enter: send the typed text, or the recording when the text box is empty."""
    if not (message or "").strip():
        if audio_path:
            # Voice turn: clear the text box and the recorder so the same clip is not sent twice.
            return send_voice(audio_path, history, request) + ("", None)
        # Nothing to send: ask for a request in the chat (in Arabic if the last reply was Arabic), keep the
        # other outputs as they are, and clear `speech` (index 8) so the old reply is not replayed.
        last = next((h["content"] for h in reversed(history or []) if h.get("role") == "assistant"), "")
        if isinstance(last, list):  # Gradio 6: [{"type": "text", "text": ...}]
            last = " ".join(p.get("text", "") for p in last if isinstance(p, dict))
        ar = any("؀" <= ch <= "ۿ" for ch in str(last))
        prompt = {"role": "assistant", "content": "من فضلك أدخل طلبك." if ar else "Please enter your request."}
        return ((history or []) + [prompt],) + tuple("" if i == 8 else gr.skip() for i in range(1, 11))
    history = history + [{"role": "user", "content": message}]
    data = post("/api/chat", json={"session_id": session_id(request), "message": message})
    return show(data, history) + ("", gr.skip())


def send_clicked(stopping: bool, message: str, audio_path: str | None, history: list, request: gr.Request):
    """Send while recording: do nothing here, the mic's stop_recording event sends the clip."""
    if stopping:
        return tuple("" if i == 8 else gr.skip() for i in range(11))
    return send_text(message, audio_path, history, request)


def recording_stopped(send_after_stop: bool, message: str, audio_path: str | None, history: list,
                      request: gr.Request):
    """Mic stopped: send only when the stop came from the Send button, not from the mic's own Stop."""
    if not send_after_stop:
        return tuple("" if i == 8 else gr.skip() for i in range(12))
    return send_text(message, audio_path, history, request) + (False,)


def send_voice(audio_path: str | None, history: list, request: gr.Request):
    if not audio_path:
        raise gr.Error("Record or upload a recording first.")
    with open(audio_path, "rb") as fh:
        data = post("/api/voice", data={"session_id": session_id(request)},
                    files={"audio": (Path(audio_path).name, fh, "audio/wav")})
    heard = data.get("transcript") or "(nothing heard)"
    return show(data, history + [{"role": "user", "content": f"(voice) {heard}"}])


def decide(pending: dict | None, history: list, request: gr.Request, approved: bool):
    if not pending:
        raise gr.Error("Nothing is waiting for approval.")
    history = history + [{"role": "user", "content": ("Approved: " if approved else "Rejected: ") + pending.get("summary", "")}]
    data = post("/api/approve", json={"session_id": session_id(request), "action_id": pending.get("action_id"),
                                      "approved": approved})
    return show(data, history)


def approve(pending, history, request: gr.Request):
    return decide(pending, history, request, True)


def reject(pending, history, request: gr.Request):
    return decide(pending, history, request, False)


def _signed_in(data: dict, request: gr.Request) -> tuple:
    """Outputs for `login_outs` after a sign-in attempt."""
    if not data.get("ok"):
        heard = f"  \n_I heard: \"{data['transcript']}\"_" if data.get("transcript") else ""
        msg = (data.get("message") or data.get("reply") or "Sign-in failed, please try again.") + heard
        pending = data.get("phone") if data.get("need_name") else gr.skip()
        return (gr.skip(),) * 4 + (f"**{msg}**", pending, gr.skip(), None) + (gr.skip(),) * 3
    LOGINS[request.session_hash or "local"] = {k: data[k] for k in ("phone", "name", "session_id")}
    messages, today = data.get("history") or [], date.today().isoformat()
    chat = _day_chat(messages, today) or [{"role": "assistant", "content":
                                           f"Welcome, {data['name'].split()[0]}! How can I help you today?"}]
    return (gr.update(visible=False), gr.update(visible=True), f"### \U0001F464 {data['name']}  \n{data['phone']}",
            gr.update(visible=True), "", None, "", None, chat,
            gr.update(choices=_day_choices(messages), value=today), _appt_rows(data.get("appointments") or []))


def login_text(text: str, pending_phone: str | None, request: gr.Request):
    if not (text or "").strip():
        return _signed_in({"message": "Please type or say your phone number first."}, request)
    return _signed_in(post("/api/login", json={"text": text, "phone": pending_phone}), request)


def login_voice(audio_path: str | None, pending_phone: str | None, request: gr.Request):
    if not audio_path:
        return (gr.skip(),) * 11
    with open(audio_path, "rb") as fh:
        data = post("/api/login/voice", data={"phone": pending_phone or ""},
                    files={"audio": (Path(audio_path).name, fh, "audio/wav")})
    return _signed_in(data, request)


def logout(request: gr.Request):
    LOGINS.pop(request.session_hash or "local", None)
    return (gr.update(visible=True), gr.update(visible=False), "", gr.update(visible=False), "", None, "", None,
            [], gr.update(choices=[], value=None), _appt_rows([]),
            "", "**State:** -", "_none_", "", None, gr.update(visible=False), gr.update(visible=False), "", "")


def show_day(day: str, request: gr.Request):
    user = LOGINS.get(request.session_hash or "local")
    if not user or not day:
        return gr.skip()
    return _day_chat(get(f"/api/history/{user['phone']}").get("messages") or [], day)


def refresh_side(day: str, request: gr.Request):
    """After every turn: today's date in the picker, fresh appointments, and today's chat if an old day was open."""
    user = LOGINS.get(request.session_hash or "local")
    if not user:
        return gr.skip(), gr.skip(), gr.skip()
    messages, today = get(f"/api/history/{user['phone']}").get("messages") or [], date.today().isoformat()
    chat = _day_chat(messages, today) if day != today else gr.skip()
    appts = get(f"/api/appointments/{user['phone']}").get("appointments")
    return (chat, gr.update(choices=_day_choices(messages), value=today),
            _appt_rows(appts) if appts is not None else gr.skip())


with gr.Blocks(title="Clinic receptionist") as demo:
    with gr.Row(equal_height=True):
        gr.Markdown(f"## Clinic receptionist\nConnected to `{APP_URL}`. Speak or type in English or Arabic.")
        user_md = gr.Markdown(elem_id="user-name")
        logout_btn = gr.Button("Sign out", visible=False, size="sm", scale=0, min_width=100)
    pending_state = gr.State(None)
    login_phone = gr.State(None)  # phone from an attempt that still needs the new patient's name
    speech = gr.Textbox(visible=False)
    send_after_stop = gr.Checkbox(False, visible=False)  # set by STOP_RECORDING_JS when Send stops the mic

    with gr.Column(visible=True) as login_box:
        gr.Markdown("### Sign in\nType or say your **phone number**. New patients: add your name too, "
                    "for example *My name is Sara Khalil, 0599123456*.")
        login_msg = gr.Markdown()
        with gr.Row():
            login_box_text = gr.Textbox(placeholder="Phone number (and your name if you are new)",
                                        show_label=False, scale=5)
            login_btn = gr.Button("Sign in", variant="primary", scale=1)
        login_mic = gr.Audio(sources=["microphone"], type="filepath",
                             label="Or say it: you are signed in when you stop recording")

    with gr.Column(visible=False) as main_box:
        with gr.Row():
            with gr.Column(scale=3):
                day_dd = gr.Dropdown(label="Conversation date", choices=[], interactive=True)
                chat = gr.Chatbot(label="Conversation", height=430)
                with gr.Row():
                    msg = gr.Textbox(placeholder="Type a message and press Enter", show_label=False, scale=5)
                    send = gr.Button("Send", variant="primary", scale=1)
                mic = gr.Audio(sources=["microphone", "upload"], type="filepath", elem_id="mic",
                               label="Speak: record or upload a WAV/MP3 file, then press Send")
            with gr.Column(scale=2):
                transcript = gr.Textbox(label="Transcript (what your app heard)", interactive=False)
                status = gr.Markdown("**State:** -")
                pending_md = gr.Markdown()
                with gr.Row():
                    approve_btn = gr.Button("Approve", variant="primary", visible=False)
                    reject_btn = gr.Button("Reject", variant="stop", visible=False)
                gr.Markdown("**Tool calls in this turn**")
                tools = gr.Markdown("_none_")
                gr.Markdown("### Your appointments")
                appts = gr.Dataframe(value=_appt_rows([]), headers=APPT_HEADERS, interactive=False, wrap=True,
                                     column_widths=["30%", "14%", "28%", "28%"])

    outs = [chat, transcript, status, tools, pending_md, pending_state, approve_btn, reject_btn, speech]
    side = dict(fn=refresh_side, inputs=[day_dd], outputs=[chat, day_dd, appts])
    # The recording is sent by the Send button (no auto-send on stop/upload), so it is never sent twice.
    # Pressing Send while recording stops the mic first; stop_recording then sends the finished clip.
    for event in (msg.submit, send.click):
        (event(None, None, [send_after_stop], js=STOP_RECORDING_JS)
         .then(send_clicked, [send_after_stop, msg, mic, chat], outs + [msg, mic])
         .then(None, [speech], None, js=SPEAK_JS).then(**side))
    mic.stop_recording(recording_stopped, [send_after_stop, msg, mic, chat],
                       outs + [msg, mic, send_after_stop]).then(None, [speech], None, js=SPEAK_JS).then(**side)
    approve_btn.click(approve, [pending_state, chat], outs).then(None, [speech], None, js=SPEAK_JS).then(**side)
    reject_btn.click(reject, [pending_state, chat], outs).then(None, [speech], None, js=SPEAK_JS).then(**side)

    # Sign-in: typed (Enter / button) or spoken (signs in when the recording stops); Sign out resets everything.
    login_outs = [login_box, main_box, user_md, logout_btn, login_msg, login_phone, login_box_text, login_mic,
                  chat, day_dd, appts]
    for event in (login_box_text.submit, login_btn.click):
        event(login_text, [login_box_text, login_phone], login_outs)
    login_mic.stop_recording(login_voice, [login_mic, login_phone], login_outs)
    logout_btn.click(logout, None, login_outs + outs[1:] + [msg])
    day_dd.input(show_day, [day_dd], [chat])

if __name__ == "__main__":
    demo.launch(server_port=int(os.getenv("UI_PORT") or 7860))

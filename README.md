# Zaytoun Voice Receptionist — books clinic appointments by voice, only after the patient approves

A voice receptionist for **Zaytoun Family Clinic** *(fictional)*: a patient speaks (or types) a request in
English or Arabic; the agent asks only for what is missing, checks the clinic's **real** calendar through
MCP, offers real slots, reads the choice back, and books **only after the patient approves** — then
confirms by voice and on screen with the booking id the clinic system returned.

Built for the Ship Week final mini-project (Brief 1) of the JHF Agentic AI Bootcamp. The kit's original
README is kept as [KIT.md](KIT.md).

## What it does

```text
Patient   Hi, I'd like a dental check-up this Tuesday after 4. I'm Lina Haddad, 0599 000 111.
Agent     Welcome back, Lina. Tomorrow (Tuesday, November 3) after 4:00 PM: I have 4:30 PM with
          Dr. Rania Khoury or 6:00 PM with Dr. Rania Khoury. Which one suits you?
Patient   4:30 please
Agent     To confirm: a dental check-up tomorrow (Tuesday, November 3) at 4:30 PM with Dr. Rania Khoury,
          for Lina Haddad. Shall I book it?        [ Approve ] [ Reject ]
Patient   (Approve, or says "yes")
Agent     Booked. Your reference is BK-1001: a dental check-up on Tuesday, November 3 at 4:30 PM with
          Dr. Rania Khoury. See you then!
```

- Voice in (speech-to-text) and voice out (Piper neural TTS, English and Arabic chosen by script).
- Taken time → the nearest real slots before and after. Closed day / after closing → says the hours.
- Returning patients recognised by phone; their preferred provider is offered first; new patients are remembered.
- Cancellations behind their own approval, with the 24-hour rule checked before any write.
- No medical advice; emergencies get "call 101".

## Architecture

```text
 Browser (Gradio UI: mic, transcript, Approve/Reject)
        │  /api/chat · /api/voice · /api/approve           (the Ship Week API contract)
        ▼
 FastAPI app (app/main.py) ── STT (OpenRouter audio model) / TTS (Piper)          [real]
        │
        ▼
 LangGraph state machine (app/agent.py)
   guard ─► context ─► INTAKE AGENT (LLM, JSON) ─┬─ book   ─► patient ─► SCHEDULER (code) ─┐
   (code)  clinic_info                           ├─ cancel ─► patient ─► cancel planner ───┤
                                                 ├─ yes/no ─► decision ─► approval gate ───┤
                                                 ├─ info   ─► CONCIERGE (LLM, facts only) ─┤
                                                 └─ other / medical / emergency ───────────┴─► speak (output check)
        │ read tools only                           ▲
        ▼                                           │ create_booking / cancel_booking ONLY from
 MCP client (app/mcp_client.py) ── stdio ──► clinic MCP server ──► clinic REST API :8101   [simulated]
                                     app/approval.py (POST /api/approve), Idempotency-Key = action_id
```

| Role | Kind | Job |
|---|---|---|
| **Input guard** | code | empty transcript, injection, emergency and medical-advice patterns before any LLM sees the text |
| **Intake agent** | LLM (`gpt-4.1-mini`, JSON validated by Pydantic) | intent + slots: service, day phrase, exact time or window, name, phone, guardian, choice |
| **Scheduler** | code + MCP read tools | resolves the day in Python from the clinic's `today`, calls `find_slots`, ranks the preferred provider, nearest alternatives, builds the `pending_action` |
| **Concierge** | LLM | answers clinic questions from `clinic_info` facts; renders replies in Arabic |
| **Approval gate** | code | the only path to `create_booking` / `cancel_booking` |
| **Output check** | code | every time in an LLM reply must come from a tool; every `BK-` id must come from the system; no medicine names |

## Run it

Python 3.12. Windows PowerShell shown.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # put OPENROUTER_API_KEY in .env; CLINIC_TODAY=2026-11-02 for the evals

# terminal 1: the simulated clinic
uvicorn services.clinic_api:app --port 8101
# terminal 2: the receptionist (starts the MCP server itself)
uvicorn app.main:app --port 8000
# terminal 3: the UI
python ui/app_ui.py         # open http://localhost:7860
```

Or everything at once: `docker compose up --build` (clinic API :8101, app :8000, UI :7860).

`python check_kit.py` ends with **✅ Kit ready.** when the clinic API, the MCP server and the voice setup answer.
`python -m voice.tts --download` fetches the Piper voices ahead of time (otherwise the first spoken reply does it).

## How it behaves — the approval gate

1. The LLM never sees a write tool. `app/mcp_client.py` refuses `create_booking` / `cancel_booking` in
   `MCP.call`; only `MCP.call_write`, used by `app/approval.py`, can reach them.
2. A complete request becomes a `pending_action` (`action_id`, `type`, `summary`, exact `payload`) stored in the
   session. One pending action at a time; a new request replaces it.
3. `POST /api/approve` (or an explicit spoken "yes", double-checked in code) executes **exactly** the stored payload
   with `Idempotency-Key = action_id`. A second Approve replays the first result: one booking, same `BK-` id.
4. `409 slot_taken` → nothing booked, nearest alternatives offered. `503` / timeout → "NOT booked", the pending
   action stays so the patient can retry safely. `result` is filled only from the system's response.

## Evaluation

26 cases: the 10 samples plus 16 of my own ([evals/my_cases.jsonl](evals/my_cases.jsonl)) — double click,
slot vanishing at approval (409), multi-turn clarification, spoken "yes", cancel inside 24 h, cancel with approval,
closed Sunday, pediatrics guardian (ask, then book), Arabic voice, spoken medical question, spoken injection,
`find_slots` outage, beyond the 21-day window, persistent memory for a new patient, and the regression case below.

```powershell
python evals/run_evals.py --cases evals/all_cases.jsonl --judge
```

On a network with a TLS-inspecting proxy, the runner's `--judge` call needs the OS certificate store:
`python -c "import truststore,runpy,sys; truststore.inject_into_ssl(); sys.argv=['x','--cases','evals/all_cases.jsonl','--judge']; runpy.run_path('evals/run_evals.py', run_name='__main__')"`
(the app itself already does this in `app/config.py`).

| | |
|---|---|
| Cases | **26 / 26 passed (100%)**, including the LLM-judged criteria |
| Turn latency | avg **2.33 s**, p95 **5.59 s** (voice turns include speech-to-text) |
| Cost of the run | **$0.0175** reported by the app (gpt-4.1-mini) |

| category | passed |
|---|---|
| happy_path | 5/5 |
| clarification | 2/2 |
| conflict | 2/2 |
| rejection | 1/1 |
| tool_failure | 2/2 |
| guardrail | 5/5 |
| memory | 2/2 |
| policy_conflict | 5/5 |
| voice | 2/2 |

Full table: [evals/scorecard.md](evals/scorecard.md).

### One failure I fixed

**Before:** while a read-back was pending, a caller who simply *repeated* the request ("I want a dental check-up
this Tuesday at 4:30…") was classified by the Intake agent as `confirm`, and the booking was written — a
booking the caller never explicitly approved.
**Fix:** "confirm" from the LLM must also pass a code check (`is_affirmative` in `app/guards.py`: yes / ok /
book it / نعم / تمام …); otherwise it is treated as a new `book` request. The intake prompt now says that
restating a request is not a confirmation.
**After:** case `M-16` (restate while pending) books nothing; `M-04` (explicit "Yes, please book it") still books.

## Guardrails and safety

- **No write without approval** — enforced by the tool split above, not by a prompt.
- **No invented ids or times** — slot ids come from `find_slots`; the output check blocks any reply with a time
  or `BK-` id that no tool returned (falls back to the code-written draft).
- **Injection** — patterns are detected in the transcript, the sentence is stripped before the LLM sees it, and the
  reply says "I can only book one appointment at a time, and only after you confirm it".
- **Medical** — emergencies get the 101 message; medicine/diagnosis questions get a refusal and an offer to book;
  replies containing medicine or diagnosis words are blocked.
- **Honest failures** — empty/garbled transcript → "Sorry, I didn't catch that"; outages say nothing was booked.
- **Dates are code** — the LLM returns a day phrase (`tuesday`, `tomorrow`); Python resolves it from the clinic's
  `today` (from `clinic_info`), never the computer's clock.

## Real vs simulated

| Component | |
|---|---|
| LLM (OpenRouter `openai/gpt-4.1-mini`) | **Real** |
| Speech-to-text (OpenRouter audio model) | **Real** |
| Text-to-speech (Piper, offline neural voices) | **Real** |
| Clinic booking system + its MCP server | **Simulated** (fictional data, real behaviour: auth, 409/422/503, idempotency) |

## Memory

- **Short-term:** each `session_id` keeps the request being built, offered slots, the pending action and the last
  turns (saved to `data/sessions.json`, so a restart doesn't lose a conversation).
- **Persistent:** the clinic's patient record via MCP — `get_patient` recognises a returning caller by phone and
  uses their preferred provider; after a booking, `save_patient` stores the language and (for new patients) the
  provider, so the next session greets them and offers that provider first.

## Cost and latency

About 1–2 LLM calls per turn (intake, plus the Concierge for clinic questions or Arabic). The full 26-case run
cost under 2 US cents. Text turns average ~2.3 s; voice turns add 2–6 s of speech-to-text.

## What I'd fix next

- Reschedule as one flow (cancel + book, each behind its own approval) instead of two separate requests.
- Use the patient's stored note ("prefers after 16:00") to pre-filter times when they don't give a window.
- Stream TTS sentence by sentence to cut voice latency; cache `clinic_info` per day instead of per session.
- A smaller model for intake, measured against the same cases.
- Docker compose is written but was not run on the build machine (no Docker there).

## Project layout

```
app/            the receptionist: main.py (API), agent.py (graph), approval.py (the only writes),
                mcp_client.py, guards.py, timeutil.py, prompts.py, llm.py, config.py
services/       the simulated clinic API + MCP server (kit, unchanged)
voice/          speech-to-text and text-to-speech adapters (kit, unchanged)
ui/app_ui.py    Gradio UI (kit, unchanged)
evals/          sample cases, my cases, all_cases.jsonl, the runner, the scorecard
DESIGN.md       states, roles, tools, the approval gate
```

## Acknowledgements

Kit, simulated clinic and eval runner: JHF Agentic AI Bootcamp (Jerusalem High-Tech Foundry, with COMCEC).
Piper voice `en_US-ljspeech-medium` (LJ Speech, public domain); `ar_JO-kareem-medium` (licence unclear, see KIT.md).
The clinic, its patients and providers are fictional.
"# finalproject_voice-receptionist" 

# DESIGN — Zaytoun Voice Receptionist

## Problem in one line
Book, and cancel, clinic appointments by voice in English and Arabic, never booking a time the patient
didn't hear and approve, never giving medical advice, and saying so honestly when the clinic system is down.

## States

```text
start ─► collect_details ─► propose_slots ─► await_confirmation ─► completed
              ▲   │               │                 │  └─(reject)──► rejected ─► (new request)
              │   └─(taken/closed)┴─► alternatives  └─(409/503)────► failed / alternatives
              └──────────────────────────────────────────────────────┘
 out_of_scope (medical, emergency, other) · info (clinic questions)
```

The API returns the current state in `state`. The graph ends at `await_confirmation` and returns the
`pending_action`; `/api/approve` executes it in plain code and the conversation continues from there.

## Typed state (per session)

| Field | Meaning |
|---|---|
| `req` | the request being built: `service_id`, `date` (ISO or `earliest`), `exact_time` / `after` / `before`, `provider_id`, `patient_name`, `phone`, `guardian_name`, `age`, `booking_id`, `mode` |
| `patient` | the clinic's record for this phone (from `get_patient`) |
| `clinic` | `clinic_info` (today, now, hours, policies) cached for the session |
| `offered` | the slots offered last turn (so "the first one" / "6 PM" resolves to a real `slot_id`) |
| `held_slot` | a chosen slot waiting only for the patient's details |
| `pending` | the one pending action: `action_id`, `type`, `summary`, exact `payload` |
| `done_actions` | executed actions by `action_id` (double-click replay) |
| `history`, `language` | last 12 turns; `en` / `ar` from the latest message's script |

## Roles

1. **Input guard (code)** — empty, injection, emergency, medical-advice patterns.
2. **Intake agent (LLM)** — structured extraction + intent; never computes dates.
3. **Scheduler (code + MCP reads)** — `find_slots`, preferred-provider ranking, nearest alternatives, read-back.
4. **Concierge (LLM)** — clinic questions from facts; Arabic rendering. Output-checked.
5. **Approval gate (code)** — the only caller of `create_booking` / `cancel_booking`.

## Tools (MCP)

| Tool | Who may call it |
|---|---|
| `clinic_info`, `list_services`, `find_slots`, `get_patient`, `get_booking` | the agent |
| `save_patient` | the approval step, after a successful booking (memory) |
| `create_booking`, `cancel_booking` | **only** `app/approval.py` from `POST /api/approve` (or a code-verified spoken "yes") |

## The approval gate
Payload stored at proposal time → executed verbatim with `Idempotency-Key = action_id` → `result` copied from the
system's response. Unknown / used `action_id` → clear reply, no write. Failure → honest reply, session continues.

## Test ideas → cases
Happy path (S1-01, M-01, M-04, M-06, M-09) · clarification (S1-02, M-03) · conflict (S1-03, M-02) · rejection
(S1-04) · tool failure (S1-05, M-13) · guardrails (S1-06, S1-10, M-11, M-12, M-16) · memory (S1-07, M-15) ·
policies (S1-09, M-05, M-07, M-08, M-14) · voice (S1-08, M-10).

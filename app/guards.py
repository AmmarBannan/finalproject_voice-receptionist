"""Guardrails in code: the input guard (injection, emergency, medical advice) and the output check.

A transcript is untrusted input. These checks run before and after the LLM, so a prompt cannot
switch them off.
"""
from __future__ import annotations

import re

from .timeutil import time_mentions

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

INJECTION_PATTERNS = [
    r"ignore (all |any |your |the )?(previous |prior |above )?(rules|instructions|prompt|guidelines)",
    r"disregard (your|the|all) (rules|instructions)",
    r"(book|reserve) (every|all) (the )?(free |available )?(slots?|appointments?|times?)",
    r"no (confirmation|approval) (needed|required|necessary)",
    r"without (asking|confirmation|approval|confirming)",
    r"(you are|act as|pretend to be) (now )?(a|an|the) (?!patient)",
    r"system prompt|developer mode|jailbreak",
    r"تجاهل (كل )?(القواعد|التعليمات)",
    r"احجز (كل|جميع) (المواعيد|الأوقات)",
]

EMERGENCY_PATTERNS = [
    r"chest pain", r"pain in (my|the) chest", r"can'?t breathe", r"cannot breathe", r"trouble breathing",
    r"difficulty breathing", r"short(ness)? of breath", r"arm (feels |is )?numb", r"numb(ness)? in (my|the) (left )?arm",
    r"unconscious", r"passed out", r"fainted", r"not breathing", r"heavy bleeding", r"bleeding (heavily|a lot|badly)",
    r"stroke", r"seizure", r"overdose", r"suicid", r"kill myself", r"severe allergic", r"anaphyla",
    r"face (is )?drooping", r"slurred speech", r"choking",
    r"ألم (شديد )?في (الصدر|صدري)", r"لا أستطيع التنفس", r"ضيق (في )?التنفس", r"فقد الوعي", r"نزيف (شديد|حاد)", r"جلطة",
]

MEDICAL_ADVICE_PATTERNS = [
    r"what (medicine|medication|drug|pill|antibiotic|painkiller)s? (should|can|do) i",
    r"which (medicine|medication|drug|pill|antibiotic|painkiller)",
    r"what should i take", r"what can i take", r"should i take", r"how (much|many) .{0,20}(take|dose)",
    r"\bdosage\b|\bdose\b", r"what (is|could) (it|this|wrong)", r"do i have (a|an)\b", r"diagnos",
    r"is it serious", r"how (do|can) i (treat|cure)", r"\b(treat|cure) (it|this|my)\b",
    r"أي (دواء|مضاد)", r"ماذا (آخذ|أتناول)", r"ما (هو )?الدواء", r"هل (هو|هذا) خطير", r"تشخيص",
]

# words the receptionist must never say on its own (medicine names, diagnoses)
BANNED_OUTPUT = [
    "aspirin", "ibuprofen", "paracetamol", "acetaminophen", "nitroglycerin", "amoxicillin", "antibiotic",
    "penicillin", "heart attack", "angina", "infection", "you may have", "you might have", "you probably have",
    "it sounds like you have", "diagnosis", "dosage", "mg ", "milligram", "advil", "tylenol", "panadol",
    "أسبرين", "مضاد حيوي", "نوبة قلبية", "باراسيتامول",
]

_RX = {name: [re.compile(p, re.I) for p in pats] for name, pats in
       {"injection": INJECTION_PATTERNS, "emergency": EMERGENCY_PATTERNS, "medical": MEDICAL_ADVICE_PATTERNS}.items()}


def _hit(kind: str, text: str) -> bool:
    return any(rx.search(text) for rx in _RX[kind])


def input_guard(text: str) -> dict:
    """Classify the raw user text before any LLM sees it."""
    t = (text or "").strip()
    return {
        "empty": len(re.sub(r"[\W_]+", "", t)) < 2,
        "too_long": len(t) > 2000,
        "injection": _hit("injection", t),
        "emergency": _hit("emergency", t),
        "medical_advice": _hit("medical", t),
        "arabic": bool(re.search(r"[؀-ۿ]", t)),
    }


def strip_injection(text: str) -> str:
    """Remove the sentences that carry an injection, keep the legitimate request."""
    parts = re.split(r"(?<=[.!?؟])\s+|\s+(?=and ignore)", text)
    kept = [p for p in parts if not _hit("injection", p)]
    return " ".join(kept).strip() or text


def find_phone(text: str) -> str | None:
    """A phone number in free text (spaces, dashes, +970, Arabic-Indic digits)."""
    t = (text or "").translate(ARABIC_DIGITS)
    for m in re.finditer(r"\+?\d[\d\s\-().]{7,18}\d", t):
        digits = re.sub(r"\D", "", m.group(0))
        if digits.startswith("972") or digits.startswith("970"):
            digits = "0" + digits[3:]
        if 9 <= len(digits) <= 10:
            return digits
    return None


def norm_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    return find_phone(str(raw)) or None


def output_check(reply: str, *, allowed_minutes: set[int], allowed_ids: set[str]) -> list[str]:
    """Problems with an LLM-written reply. Empty list = safe to send."""
    problems = []
    low = reply.lower()
    for mention in time_mentions(reply):
        if not mention & allowed_minutes:
            problems.append(f"time not from a tool: {sorted(mention)}")
    for bid in re.findall(r"BK-\d+", reply, re.I):
        if bid.upper() not in allowed_ids:
            problems.append(f"booking id not returned by the system: {bid}")
    for word in BANNED_OUTPUT:
        if word in low:
            problems.append(f"medical content: {word}")
    return problems


AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|correct|please do|go ahead|book it|that'?s (right|fine|good)|"
    r"sounds good|perfect|great|fine)\b|نعم|أجل|أيوه|تمام|موافق|أكد|حسنا|طيب", re.I)


def is_affirmative(text: str) -> bool:
    """An explicit yes. The LLM's 'confirm' must agree with this before anything is written."""
    return bool(AFFIRMATIVE.search(text or ""))

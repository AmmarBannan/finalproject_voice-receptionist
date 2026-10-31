"""Text-to-speech for the receptionist: Azure AI Speech neural voices over REST.

    speak(text, language="en") -> (audio_bytes, mime_type) | None

Voices: English en-US-JennyNeural, Arabic ar-JO-SanaNeural (Jordanian, close to the Levantine callers).
Needs AZURE_SPEECH_KEY and AZURE_SPEECH_REGION; override voices with AZURE_VOICE_EN / AZURE_VOICE_AR and
the request timeout with AZURE_TTS_TIMEOUT (seconds, default 10). Returns 24 kHz 16-bit mono WAV.

The voice follows the text's script: Arabic letters -> Arabic voice, otherwise English.
`language` is only used when the text has no letters. speak() returns None (and logs a
warning) when synthesis fails, so a reply is never lost because of TTS; it is just not spoken.

    python -m voice.tts "Your appointment is booked." --out reply.wav
    python -m voice.tts "تم حجز موعدك" --out reply.wav
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

log = logging.getLogger("voice.tts")
ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
AZURE_VOICES = {"en": "en-US-JennyNeural", "ar": "ar-JO-SanaNeural"}


def detect_language(text: str, default: str = "en") -> str:
    """'ar' when at least 30% of the letters are Arabic, else 'en' (default when there are no letters)."""
    letters = [c for c in text or "" if c.isalpha()]
    if not letters:
        return default if default in ("en", "ar") else "en"
    return "ar" if sum(bool(ARABIC.match(c)) for c in letters) / len(letters) >= 0.3 else "en"


def azure_voice(language: str) -> str:
    return os.getenv(f"AZURE_VOICE_{language.upper()}") or AZURE_VOICES["ar" if language == "ar" else "en"]


def _speakable(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`>|\[\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:1000]


def _azure(text: str, language: str) -> bytes:
    """Azure AI Speech REST API -> 24 kHz 16-bit mono WAV bytes. Raises on any failure."""
    key, region = os.getenv("AZURE_SPEECH_KEY"), os.getenv("AZURE_SPEECH_REGION")
    if not key or not region:
        raise RuntimeError("AZURE_SPEECH_KEY and AZURE_SPEECH_REGION must be set")
    voice = azure_voice(language)
    locale = "-".join(voice.split("-")[:2])  # ar-JO-SanaNeural -> ar-JO
    ssml = (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='{locale}'>"
            f"<voice name='{voice}'>{escape(text)}</voice></speak>")
    r = httpx.post(f"https://{region.strip()}.tts.speech.microsoft.com/cognitiveservices/v1",
                   content=ssml.encode("utf-8"), timeout=float(os.getenv("AZURE_TTS_TIMEOUT") or 10),
                   headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                            "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
                            "User-Agent": "clinic-receptionist"})
    if r.status_code != 200 or not r.content:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:200]}")
    return r.content


def speak(text: str, language: str = "en") -> tuple[bytes, str] | None:
    """Synthesize `text`. Returns (wav_bytes, "audio/wav"), or None on failure."""
    text = _speakable(text or "")
    if not text:
        return None
    try:
        return _azure(text, detect_language(text, language)), "audio/wav"
    except Exception as exc:  # noqa: BLE001 - never break a reply because of TTS
        log.warning("Azure TTS failed (%s); no audio returned", exc)
        return None


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description="Azure text-to-speech for the receptionist")
    ap.add_argument("text", help="text to speak (Arabic or English)")
    ap.add_argument("--out", default="tts_test.wav", help="output WAV file")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    out = speak(args.text)
    if not out:
        sys.exit("No audio produced (see the warning above).")
    Path(args.out).write_bytes(out[0])
    print(f"Wrote {args.out} ({len(out[0]) // 1024} KB, voice {azure_voice(detect_language(args.text))})")


if __name__ == "__main__":
    main()

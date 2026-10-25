import json, time, httpx, uuid, sys
out = []
for f in ["samples/audio/en_02_vague_friday.wav", "samples/audio/en_01_book_tuesday.wav"]:
    t = time.perf_counter()
    try:
        with open(f, "rb") as fh:
            r = httpx.post("http://localhost:8000/api/voice", timeout=180,
                           files={"audio": (f.split("/")[-1], fh, "audio/wav")},
                           data={"session_id": "voicetest-" + uuid.uuid4().hex[:6]})
        d = r.json()
        out.append({"file": f, "status": r.status_code, "seconds": round(time.perf_counter() - t, 1),
                    "transcript": d.get("transcript"), "reply": d.get("reply"),
                    "tool_calls": d.get("tool_calls"), "has_audio": bool(d.get("audio_b64"))})
    except Exception as e:
        out.append({"file": f, "error": repr(e), "seconds": round(time.perf_counter() - t, 1)})
open("_to_delete_voice_test.json", "w", encoding="utf-8").write(json.dumps(out, indent=1, ensure_ascii=False))

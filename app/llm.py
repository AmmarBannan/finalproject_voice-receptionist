"""Thin OpenRouter client: JSON outputs validated by Pydantic, token usage accumulated per turn."""
from __future__ import annotations

import asyncio
import json
import time
from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from .config import MODEL, OPENROUTER_API_KEY, OPENROUTER_BASE_URL, PRICE_IN, PRICE_OUT, log

T = TypeVar("T", bound=BaseModel)

_client = AsyncOpenAI(api_key=OPENROUTER_API_KEY or "missing", base_url=OPENROUTER_BASE_URL, timeout=40)


def new_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}


def _add_usage(usage: dict, resp) -> None:
    u = getattr(resp, "usage", None)
    if not u:
        return
    usage["prompt_tokens"] += u.prompt_tokens or 0
    usage["completion_tokens"] += u.completion_tokens or 0
    usage["cost_usd"] = round(usage["prompt_tokens"] * PRICE_IN / 1e6
                              + usage["completion_tokens"] * PRICE_OUT / 1e6, 6)


async def _complete(messages: list[dict], usage: dict, *, json_mode: bool, max_tokens: int, role: str) -> str:
    last_exc: Exception | None = None
    for attempt in range(3):
        t0 = time.perf_counter()
        try:
            resp = await _client.chat.completions.create(
                model=MODEL, messages=messages, temperature=0, max_tokens=max_tokens,
                **({"response_format": {"type": "json_object"}} if json_mode else {}))
            _add_usage(usage, resp)
            log.info("llm %s %dms", role, int((time.perf_counter() - t0) * 1000))
            return resp.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 - rate limit or network: back off and retry
            last_exc = exc
            log.warning("llm %s attempt %d failed: %s", role, attempt + 1, exc)
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"LLM unavailable: {last_exc}")


async def structured(system: str, user: str, model: type[T], usage: dict, role: str) -> T:
    """Ask for JSON matching `model`; one repair attempt if it does not validate."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    text = await _complete(messages, usage, json_mode=True, max_tokens=600, role=role)
    try:
        return model.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": f"That JSON was invalid ({str(exc)[:300]}). Reply with valid JSON only."}]
        text = await _complete(messages, usage, json_mode=True, max_tokens=600, role=role + "-repair")
        return model.model_validate(json.loads(text))


async def text(system: str, user: str, usage: dict, role: str, max_tokens: int = 300) -> str:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return (await _complete(messages, usage, json_mode=False, max_tokens=max_tokens, role=role)).strip()

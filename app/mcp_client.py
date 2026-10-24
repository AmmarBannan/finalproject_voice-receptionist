"""One long-lived MCP session to the clinic's MCP server (stdio), shared by the whole app.

Every call is timed and recorded in the turn's `tool_calls` list. Only READ tools and the memory
tool `save_patient` are exposed to the agent; `create_booking` and `cancel_booking` are reachable
only through `call_write`, which the approval endpoint uses.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import ROOT, log

AGENT_TOOLS = {"clinic_info", "list_services", "find_slots", "get_patient", "get_booking", "save_patient"}
WRITE_TOOLS = {"create_booking", "cancel_booking"}


class ClinicMCP:
    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._lock = asyncio.Lock()
        self.tools: list[str] = []

    async def start(self) -> None:
        async with self._lock:
            if self._session:
                return
            params = StdioServerParameters(command=sys.executable, args=["-m", "services.clinic_mcp"],
                                           env={**os.environ}, cwd=str(ROOT))
            stack = AsyncExitStack()
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.tools = [t.name for t in (await session.list_tools()).tools]
            self._stack, self._session = stack, session
            log.info("MCP connected, tools: %s", ", ".join(self.tools))

    async def stop(self) -> None:
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
        self._stack = self._session = None

    async def _raw(self, name: str, args: dict) -> dict:
        if not self._session:
            await self.start()
        try:
            res = await asyncio.wait_for(self._session.call_tool(name, args), timeout=30)
        except Exception as exc:  # noqa: BLE001 - the server died: restart once and retry
            log.warning("MCP call %s failed (%s), reconnecting", name, exc.__class__.__name__)
            await self.stop()
            await self.start()
            res = await asyncio.wait_for(self._session.call_tool(name, args), timeout=30)
        data = res.structuredContent
        if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], dict):
            data = data["result"]
        if not isinstance(data, dict):
            text = "".join(getattr(c, "text", "") for c in res.content)
            try:
                data = json.loads(text)
            except ValueError:
                data = {"ok": False, "error": {"code": "bad_response", "message": text[:200]}}
        return data

    async def _timed(self, name: str, args: dict, calls: list) -> dict:
        t0 = time.perf_counter()
        try:
            data = await self._raw(name, args)
        except Exception as exc:  # noqa: BLE001 - report as a failed tool call, never crash the turn
            data = {"ok": False, "error": {"code": "mcp_error", "message": str(exc)[:200], "retryable": True}}
        ms = int((time.perf_counter() - t0) * 1000)
        ok = bool(data.get("ok"))
        entry: dict[str, Any] = {"tool": name, "ok": ok, "ms": ms}
        if not ok:
            entry["error"] = (data.get("error") or {}).get("code")
        calls.append(entry)
        log.info("tool %s %s %dms %s", name, "ok" if ok else "FAILED", ms,
                 "" if ok else json.dumps(data.get("error"))[:200])
        return data

    async def call(self, name: str, args: dict, calls: list) -> dict:
        """A tool the agent may use. Write tools are refused here, in code."""
        if name not in AGENT_TOOLS:
            raise PermissionError(f"{name} is not available to the agent")
        return await self._timed(name, {k: v for k, v in args.items() if v is not None}, calls)

    async def call_write(self, name: str, args: dict, calls: list) -> dict:
        """Only the approval step calls this, with an approved payload."""
        if name not in WRITE_TOOLS:
            raise PermissionError(name)
        return await self._timed(name, {k: v for k, v in args.items() if v is not None}, calls)


MCP = ClinicMCP()

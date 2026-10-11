"""First-run name for the handheld client.

The name has to land in MEMORY.md before the first session exists. Memory is
copied into the system prompt when a session starts, and that prefix stays
frozen for the life of the conversation.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes_cli.web_routers._common import config_scoped_to_thread, destructive_profile

router = APIRouter()

_MAX_NAME = 24


class PocketNameBody(BaseModel):
    name: str


def name_memory_line(name: str) -> str:
    """The single memory entry for a name the user just chose.

    Raises ValueError when the name is empty, too long, or contains a
    character that would split or poison a memory file.
    """
    raw = name or ""
    if any(ord(char) < 32 or char in "§<>" for char in raw):
        raise ValueError("这个名字记不下来")
    cleaned = " ".join(raw.split())
    if not cleaned or len(cleaned) > _MAX_NAME:
        raise ValueError("名字空着，或超过 24 个字")
    return f"我的名字是{cleaned}，这是用户给我起的。"


def _store_line(line: str) -> None:
    from tools.memory_tool_store import MemoryStore

    result = MemoryStore().add("memory", line)
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=str(result.get("error") or "名字没有记下"))


@router.post("/api/pocket/name")
async def remember_pocket_name(body: PocketNameBody, profile: Optional[str] = None):
    try:
        line = name_memory_line(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    scoped = destructive_profile(profile, "POST /api/pocket/name")
    await config_scoped_to_thread(scoped, lambda: _store_line(line))
    return {"ok": True}


@router.delete("/api/pocket/role")
async def destroy_pocket_role(body: PocketNameBody, profile: Optional[str] = None):
    """Remove the Pocket role's name memory before the client forgets it."""
    try:
        line = name_memory_line(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def remove_line() -> None:
        from tools.memory_tool_store import MemoryStore

        result = MemoryStore().remove("memory", line)
        if not result.get("success"):
            error = str(result.get("error") or "角色记忆没有清除")
            if "No entry matched" not in error:
                raise HTTPException(status_code=409, detail=error)

    scoped = destructive_profile(profile, "DELETE /api/pocket/role")
    await config_scoped_to_thread(scoped, remove_line)
    return {"ok": True}

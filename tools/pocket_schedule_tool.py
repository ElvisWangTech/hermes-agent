"""Pocket schedule tool. The model resolves language; a complete create is saved immediately."""

from __future__ import annotations

import json

from hermes_constants import get_hermes_home
from tools.registry import registry

_SCHEMA = {
    "name": "pocket_schedule",
    "description": (
        "Save or change a Pocket schedule. You resolve what the user said, in their language, "
        "into these fields. Call it for a reminder, an alarm, or something to do at a time, "
        "and for listing, pausing, resuming, cancelling, or moving one. "
        "If the hour was not stated, or a 1-12 hour had no morning/evening and was not given "
        "in 24-hour form, do not call this: ask in the user's language. "
        "Set resolved true only when the time is complete. "
        "A create with resolved true is saved by this call. Tell the user it is saved. "
        "Do not ask them to confirm a create. Pause, resume, cancel, and reschedule still "
        "open a card and are not saved until the user confirms."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "pause", "resume", "cancel", "reschedule"],
            },
            "resolved": {
                "type": "boolean",
                "description": "True only when the user stated a complete time.",
            },
            "kind": {
                "type": "string",
                "enum": ["remind", "task"],
                "description": "remind speaks the name back; task does the thing named.",
            },
            "name": {"type": "string", "description": "Short label in the user's language."},
            "when": {
                "type": "string",
                "description": "ISO 8601 datetime for a one-shot, in the user's local offset.",
            },
            "when_label": {
                "type": "string",
                "description": "How to read the time aloud, in the user's language.",
            },
            "repeat": {
                "type": "string",
                "enum": ["once", "daily", "weekdays", "weekly", "every_minutes", "every_hours", "in_minutes"],
            },
            "hour": {"type": "integer", "description": "0-23 for a repeating clock time."},
            "minute": {"type": "integer"},
            "weekday": {"type": "integer", "description": "0-6 for weekly, Sunday = 0."},
            "count": {"type": "integer", "description": "Minutes or hours for interval repeats."},
            "job_id": {"type": "string", "description": "Id from a previous list, for a change."},
        },
        "required": ["action"],
    },
}


def pocket_schedule(args: dict) -> str:
    from hermes_cli.pocket_schedule import draft

    result = draft(args if isinstance(args, dict) else {}, get_hermes_home().name)
    if result.get("type") == "saved":
        result["note"] = (
            "Already saved. Say the speech field in one short sentence in the user's language. "
            "Do not ask them to confirm or tap anything."
        )
    elif result.get("type") in {"proposal", "list"}:
        result["ok"] = True
        result["note"] = (
            "A card is on screen. Ask the user to confirm in one short sentence in their language. "
            "Do not say it is saved."
        )
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="pocket_schedule",
    toolset="pocket",
    schema=_SCHEMA,
    handler=lambda args, **kw: pocket_schedule(args),
)

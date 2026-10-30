"""Pocket schedules on top of the profile's existing cron store.

The phone confirms a proposal before anything is written. When a job fires, the
result is stored as unheard. A device marks it heard only after it has spoken
the line. Notifiers (a future app notification) are told about the result and
cannot mark it heard.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator, Optional

_NOTIFIERS: list[Callable[[dict], None]] = []
_PROPOSAL_TTL = 600
_LEASE_SECONDS = 120
_HOUR_WORDS = (
    ("十二", 12), ("十一", 11), ("十", 10), ("九", 9), ("八", 8), ("七", 7),
    ("六", 6), ("五", 5), ("四", 4), ("三", 3), ("两", 2), ("二", 2), ("一", 1), ("零", 0),
)
_WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 0, "天": 0, "7": 0, "0": 0}
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_INT = (
    r"三十[一]?|二十[一二三四五六七八九]?|十[一二三四五六七八九]?|[一二三四五六七八九两]"
)


def schedule_limit() -> int:
    """Unfinished schedules allowed for the active profile. Blank or invalid uses 20."""
    from hermes_cli.config import load_config

    raw = (load_config().get("pocket") or {}).get("schedule_limit", 20)
    if raw is None or raw == "":
        return 20
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 20
    return value if value > 0 else 20


def reminder_phrase(job: dict) -> Optional[str]:
    """The fixed sentence for a Pocket reminder, or None when the job must run."""
    origin = job.get("origin")
    if not isinstance(origin, dict):
        return None
    if str(origin.get("platform") or "").lower() != "pocket":
        return None
    if origin.get("pocket_kind") != "remind":
        return None
    phrase = str(origin.get("pocket_phrase") or "").strip()
    return phrase or None


def register_schedule_notifier(fn: Callable[[dict], None]) -> None:
    """Observe a newly stored result. The callback must not mark it heard.

    The in-app page is the only speaker today. An embedded app can register
    here to raise a system notification from the same result.
    """
    _NOTIFIERS.append(fn)


def unregister_schedule_notifier(fn: Callable[[dict], None]) -> None:
    if fn in _NOTIFIERS:
        _NOTIFIERS.remove(fn)


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    from cron.jobs import get_cron_output_dir

    directory = get_cron_output_dir().parent
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "pocket-schedule.sqlite"
    # Autocommit so BEGIN IMMEDIATE is the transaction, not a nested one.
    database = sqlite3.connect(path, timeout=10, isolation_level=None)
    database.row_factory = sqlite3.Row
    try:
        database.executescript("""
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                expires REAL NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS inbox (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                text TEXT NOT NULL,
                ok INTEGER NOT NULL,
                created_at REAL NOT NULL,
                heard_at REAL,
                leased_by TEXT,
                leased_until REAL
            );
        """)
        with database:
            yield database
    finally:
        database.close()
        path.chmod(0o600)


def _is_pocket(job: dict) -> bool:
    origin = job.get("origin")
    return isinstance(origin, dict) and str(origin.get("platform") or "").lower() == "pocket"


def _jobs() -> list[dict]:
    from cron.jobs import is_terminal_job, load_jobs

    return [job for job in load_jobs() if _is_pocket(job) and not is_terminal_job(job)]


def _views() -> list[dict]:
    views = []
    for job in _jobs():
        origin = job.get("origin") or {}
        paused = not job.get("enabled", True) or job.get("state") == "paused" or bool(job.get("paused_at"))
        views.append({
            "id": job["id"],
            "name": str(job.get("name") or ""),
            "phrase": str(origin.get("pocket_phrase") or ""),
            "when": str(origin.get("pocket_when") or ""),
            "paused": paused,
            "job": job,
        })
    return views


def _run_is_past(value, now: datetime) -> bool:
    if not value:
        return True
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=now.tzinfo)
    return when <= now


def _ordered_views() -> list[dict]:
    """Newer current todos first. A past next run is expired and sorts last."""
    import hermes_time

    now = hermes_time.now()
    views = _views()
    for view in views:
        view["created_at"] = str(view["job"].get("created_at") or "")
        view["expired"] = (not view["paused"]) and _run_is_past(view["job"].get("next_run_at"), now)
    views.sort(key=lambda view: view["created_at"], reverse=True)
    views.sort(key=lambda view: view["expired"])
    return views


def timezone_label() -> str:
    import hermes_time

    name = hermes_time.get_timezone_name()
    return name or "服务器本地时间"


def _store_proposal(payload: dict, *, title: str, body: str, allow: str, deny: str) -> dict:
    proposal_id = secrets.token_hex(16)
    with _db() as database:
        database.execute(
            "INSERT INTO proposals(id, payload, expires) VALUES (?, ?, ?)",
            (proposal_id, json.dumps(payload, ensure_ascii=False), time.time() + _PROPOSAL_TTL),
        )
    return {
        "type": "proposal",
        "id": proposal_id,
        "title": title,
        "body": body,
        "allow": allow,
        "deny": deny,
    }


def draft(spec: dict, profile_label: str) -> dict:
    """Apply a model's structured decision. A complete create is saved immediately."""
    action = str(spec.get("action") or "create")
    if action == "list":
        views = _ordered_views()
        return {
            "type": "list",
            "speech": _list_speech(views),
            "items": [
                {"id": view["id"], "name": view["name"], "when": view["when"], "paused": view["paused"]}
                for view in views
            ],
        }
    if action in {"pause", "resume", "cancel", "reschedule"}:
        return _draft_change(spec, profile_label, action)
    if action != "create":
        return {"ok": False, "error": "Unknown action. Ask the user what they want done with the schedule."}
    if spec.get("resolved") is not True:
        return {
            "ok": False,
            "error": "The time is incomplete. Ask the user in their language. Do not invent an hour or morning versus evening.",
        }
    built = _schedule_from_spec(spec)
    if "error" in built:
        return {"ok": False, "error": built["error"]}
    kind = spec.get("kind")
    name = " ".join(str(spec.get("name") or "").split())
    if kind not in {"remind", "task"} or not name:
        return {"ok": False, "error": "Say what should happen at that time, in the user's language, then call again."}
    payload = {
        "action": "create",
        "kind": kind,
        "schedule": built["schedule"],
        "when": built["when"],
        "name": name[:24],
        "phrase": name if kind == "remind" else "",
        "task": name if kind == "task" else "",
        "repeat": spec.get("repeat_count"),
        "zone": timezone_label(),
        "profile": profile_label,
    }
    try:
        _require_room()
        job = _create_job(payload)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "type": "saved",
        "speech": f"记下了。{built['when']}",
        "id": job["id"],
    }


def interpret(text: str, profile_label: str) -> dict:
    """Read an utterance. A proposal is the only path that can later write a job."""
    import hermes_time

    cleaned = " ".join((text or "").split())
    if not cleaned or len(cleaned) > 400:
        return {"type": "chat"}
    parsed = parse_utterance(cleaned, _views(), zone=timezone_label(), now=hermes_time.now())
    if parsed["type"] != "proposal":
        if parsed["type"] == "list":
            views = _ordered_views()
            parsed["speech"] = _list_speech(views)
        return parsed
    payload = dict(parsed["payload"])
    payload["profile"] = profile_label
    return _store_proposal(
        payload,
        title=parsed["title"],
        body=parsed["body"],
        allow=parsed["allow"],
        deny=parsed["deny"],
    )


def confirm(proposal_id: str) -> dict:
    """Consume one proposal and apply it. A second confirm does not write again."""
    payload = _peek(proposal_id)
    if payload.get("action") == "create":
        _require_room()
    payload = _consume(proposal_id)
    action = payload.get("action")
    if action == "create":
        job = _create_job(payload)
        return {"ok": True, "speech": f"记下了。{payload.get('when') or job.get('name')}", "id": job["id"]}
    view = _find(str(payload.get("job_id") or ""))
    if view is None:
        raise ValueError("没有找到这条日程")
    if action == "pause":
        _pause(view["id"])
        return {"ok": True, "speech": "停掉了。", "id": view["id"]}
    if action == "resume":
        nxt = _resume(view["job"])
        return {"ok": True, "speech": f"继续了。下次是{nxt}", "id": view["id"]}
    if action == "remove":
        _remove(view["id"])
        return {"ok": True, "speech": "取消了。", "id": view["id"]}
    if action == "reschedule":
        _reschedule(view["job"], payload)
        return {"ok": True, "speech": f"改好了。{payload.get('when')}", "id": view["id"]}
    raise ValueError("这张卡片不能用")


def decline(proposal_id: str) -> dict:
    _consume(proposal_id)
    return {"ok": True, "speech": "没记下。"}


def list_schedules() -> dict:
    views = _ordered_views()
    last = _latest_by_job()
    items = []
    for view in views:
        recent = last.get(view["id"])
        items.append({
            "id": view["id"],
            "name": view["name"],
            "when": view["when"],
            "next": "" if view["paused"] else _next_label(view["job"].get("next_run_at")),
            "paused": view["paused"],
            "expired": view["expired"],
            "created_at": view["created_at"],
            "last_text": recent["text"] if recent else "",
            "last_ok": recent["ok"] if recent else None,
        })
    with _db() as database:
        unheard = [
            {"id": row["id"], "text": row["text"], "created_at": row["created_at"]}
            for row in database.execute(
                "SELECT id, text, created_at FROM inbox WHERE heard_at IS NULL ORDER BY created_at"
            )
        ]
        said = [
            {"text": row["text"], "ok": bool(row["ok"])}
            for row in database.execute(
                "SELECT text, ok FROM inbox WHERE heard_at IS NOT NULL ORDER BY heard_at DESC LIMIT 5"
            )
        ]
    return {"items": items, "unheard": unheard, "said": said}


def claim(device_id: str, ids: list[str]) -> list[dict]:
    """Lease unheard lines for one device. A live lease is not given out twice."""
    if not device_id or not ids:
        return []
    now = time.time()
    claimed = []
    with _db() as database:
        database.execute("BEGIN IMMEDIATE")
        for item_id in ids[:3]:
            updated = database.execute(
                """
                UPDATE inbox
                   SET leased_by = ?, leased_until = ?
                 WHERE id = ?
                   AND heard_at IS NULL
                   AND (leased_until IS NULL OR leased_until <= ? OR leased_by = ?)
                """,
                (device_id, now + _LEASE_SECONDS, item_id, now, device_id),
            )
            if updated.rowcount != 1:
                continue
            row = database.execute(
                "SELECT id, text, ok FROM inbox WHERE id = ?", (item_id,)
            ).fetchone()
            if row is not None:
                claimed.append({"id": row["id"], "text": row["text"], "ok": bool(row["ok"])})
    return claimed


def settle(item_id: str, device_id: str, *, delivered: bool) -> None:
    """Mark a line heard after this device spoke it, or release the lease if it did not."""
    with _db() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute("SELECT * FROM inbox WHERE id = ?", (item_id,)).fetchone()
        if row is None or row["leased_by"] != device_id:
            raise ValueError("这条还不能由这台设备收下")
        if delivered:
            database.execute(
                "UPDATE inbox SET heard_at = ?, leased_until = NULL WHERE id = ? AND heard_at IS NULL",
                (time.time(), item_id),
            )
        else:
            database.execute(
                "UPDATE inbox SET leased_by = NULL, leased_until = NULL WHERE id = ? AND heard_at IS NULL",
                (item_id,),
            )


def deliver_to_pocket(job: dict, content: str, *, ok: bool) -> None:
    """Store one fired result, then tell notifiers. Hearing it is a separate step."""
    if not _is_pocket(job):
        return
    name = str(job.get("name") or "这件事")
    if ok:
        text = (content or "").strip()
        if not text:
            return
    else:
        if _failure_already_noted(str(job.get("id") or "")):
            return
        text = f"刚才没能完成{name}"
    item = {
        "id": secrets.token_hex(16),
        "job_id": str(job.get("id") or ""),
        "text": text,
        "ok": bool(ok),
    }
    with _db() as database:
        database.execute(
            "INSERT INTO inbox(id, job_id, text, ok, created_at) VALUES (?, ?, ?, ?, ?)",
            (item["id"], item["job_id"], item["text"], int(ok), time.time()),
        )
    for notify in list(_NOTIFIERS):
        notify(dict(item))


def hold_for_authorization() -> int:
    """Pause active Pocket schedules. Calling this again does not resume them."""
    from cron.jobs import pause_job

    held = 0
    for view in _views():
        if view["paused"]:
            continue
        pause_job(view["id"], "authorization")
        held += 1
    return held


def clear_schedules() -> None:
    """Remove this profile's Pocket schedules and unheard lines."""
    from cron.jobs import remove_job

    for view in list(_views()):
        remove_job(view["id"])
    from cron.jobs import get_cron_output_dir

    path = get_cron_output_dir().parent / "pocket-schedule.sqlite"
    if path.exists():
        path.unlink()


def _schedule_from_spec(spec: dict) -> dict:
    """Validate a resolved clock. The model already chose the calendar meaning."""
    import hermes_time

    repeat = str(spec.get("repeat") or "once")
    label = " ".join(str(spec.get("when_label") or "").split())
    if repeat == "once":
        raw = str(spec.get("when") or "")
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            return {"error": "when must be an ISO 8601 datetime."}
        now = hermes_time.now()
        if when.tzinfo is None:
            when = when.replace(tzinfo=now.tzinfo)
        if when <= now:
            return {"error": "That time is already past. Ask for a future time."}
        return {"schedule": when.isoformat(), "when": label or when.strftime("%Y-%m-%d %H:%M")}
    if repeat in {"every_minutes", "every_hours", "in_minutes"}:
        try:
            count = int(spec.get("count"))
        except (TypeError, ValueError):
            return {"error": "count must be a positive number of minutes or hours."}
        if count <= 0:
            return {"error": "count must be a positive number of minutes or hours."}
        schedules = {
            "every_minutes": f"{count}m",
            "every_hours": f"every {count}h",
            "in_minutes": f"in {count}m",
        }
        fallback = {
            "every_minutes": f"every {count} minutes",
            "every_hours": f"every {count} hours",
            "in_minutes": f"in {count} minutes",
        }
        return {"schedule": schedules[repeat], "when": label or fallback[repeat]}
    clock = _clock_numbers(spec)
    if "error" in clock:
        return clock
    minute, hour = clock["minute"], clock["hour"]
    if repeat == "daily":
        schedule = f"{minute} {hour} * * *"
    elif repeat == "weekdays":
        schedule = f"{minute} {hour} * * 1-5"
    elif repeat == "weekly":
        try:
            dow = int(spec.get("weekday"))
        except (TypeError, ValueError):
            return {"error": "weekly needs weekday 0-6, Sunday = 0."}
        if not 0 <= dow <= 6:
            return {"error": "weekly needs weekday 0-6, Sunday = 0."}
        schedule = f"{minute} {hour} * * {dow}"
    else:
        return {"error": "repeat must be once, daily, weekdays, weekly, every_minutes, every_hours, or in_minutes."}
    return {"schedule": schedule, "when": label or f"{hour:02d}:{minute:02d}"}


def _clock_numbers(spec: dict) -> dict:
    try:
        hour = int(spec.get("hour"))
        minute = int(spec.get("minute") or 0)
    except (TypeError, ValueError):
        return {"error": "hour and minute must be numbers."}
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return {"error": "hour must be 0-23 and minute 0-59."}
    return {"hour": hour, "minute": minute}


def _draft_change(spec: dict, profile_label: str, action: str) -> dict:
    view = _find(str(spec.get("job_id") or ""))
    if view is None:
        matches = _match_jobs(str(spec.get("name") or ""), _views())
        if len(matches) != 1:
            return {"ok": False, "error": "Name the schedule from the list, or pass its job_id."}
        view = matches[0]
    zone = timezone_label()
    if action == "reschedule":
        if spec.get("resolved") is not True:
            return {
                "ok": False,
                "error": "The new time is incomplete. Ask the user in their language. Do not invent an hour.",
            }
        built = _schedule_from_spec(spec)
        if "error" in built:
            return {"ok": False, "error": built["error"]}
        return _store_proposal(
            {
                "action": "reschedule",
                "job_id": view["id"],
                "schedule": built["schedule"],
                "when": built["when"],
                "zone": zone,
                "profile": profile_label,
            },
            title="改成这样？",
            body=f"{view['name']}\n{built['when']}\n{zone}",
            allow="改好",
            deny="取消",
        )
    stored = "remove" if action == "cancel" else action
    titles = {
        "pause": (f"先停掉「{view['name']}」？", view["when"], "停掉", "留下"),
        "resume": (f"继续「{view['name']}」？", view["when"], "继续", "取消"),
        "remove": (f"取消「{view['name']}」？", "取消后不再提醒。", "取消这条", "留下"),
    }
    title, body, allow, deny = titles[stored]
    return _store_proposal(
        {"action": stored, "job_id": view["id"], "profile": profile_label},
        title=title,
        body=body,
        allow=allow,
        deny=deny,
    )


def _spoken(text: str) -> str:
    """Speech inserts spaces around digits, and 时/今晚 are the same clock as 点/晚上."""
    compact = re.sub(r"[\s\u3000]+", "", text or "")
    return compact.replace("今夜", "晚上").replace("今晚", "晚上")


def parse_utterance(text: str, jobs: list[dict], *, zone: str, now: datetime) -> dict:
    """Turn one utterance into a chat, a question, a list, or an unstored proposal."""
    text = _spoken(text)
    if re.search(r"有哪些日程|有什么日程|我的日程|看看日程|有哪些提醒|有什么提醒", text):
        return {"type": "list"}
    if re.search(r"改到|停掉|暂停|先别|取消|删掉|恢复", text) or re.search(r"继续.+(提醒|日程)", text):
        return _parse_change(text, jobs, zone=zone, now=now)
    schedule = _parse_when(text, now)
    if schedule is None and not re.search(r"提醒我|跟我说|告诉我|看看|查一下|查", text):
        return {"type": "chat"}
    if schedule is None:
        return {"type": "clarify", "question": "什么时候？", "pending": text}
    if schedule.get("clarify"):
        return {"type": "clarify", "question": schedule["clarify"], "pending": text}
    kind, subject = _intent(text)
    if not subject:
        return {"type": "clarify", "question": "要她说什么，或者做什么？", "pending": text}
    repeat = 3 if re.search(r"只提醒三次|提醒三次|共三次", text) else None
    payload = {
        "action": "create",
        "kind": kind,
        "schedule": schedule["schedule"],
        "when": schedule["when"],
        "name": subject[:24],
        "phrase": subject if kind == "remind" else "",
        "task": subject if kind == "task" else "",
        "repeat": repeat,
        "zone": zone,
    }
    doing = f"到点她会说：{subject}" if kind == "remind" else f"到点她会去做：{subject}"
    if repeat:
        doing += "，共 3 次"
    return {
        "type": "proposal",
        "title": "记下这个日程？",
        "body": f"{schedule['when']}\n{zone}\n{doing}",
        "allow": "记下",
        "deny": "取消",
        "payload": payload,
    }


def _parse_change(text: str, jobs: list[dict], *, zone: str, now: datetime) -> dict:
    action = "reschedule" if "改到" in text else None
    if action is None and re.search(r"停掉|暂停|先别", text):
        action = "pause"
    elif action is None and re.search(r"取消|删掉", text):
        action = "remove"
    elif action is None and re.search(r"继续|恢复", text):
        action = "resume"
    matches = _match_jobs(text, jobs)
    if action is None:
        return {"type": "chat"}
    if not matches:
        if re.search(r"日程|提醒", text):
            return {"type": "clarify", "question": "没有找到这条日程。", "pending": text}
        return {"type": "chat"}
    if len(matches) > 1:
        names = "、".join(item["name"] for item in matches)
        return {"type": "clarify", "question": f"有好几条：{names}。说得再具体一点。", "pending": text}
    job = matches[0]
    if action == "reschedule":
        when_text = text.split("改到", 1)[1]
        schedule = _parse_when(when_text, now)
        if schedule is None or schedule.get("clarify"):
            question = (schedule or {}).get("clarify") or "改到什么时候？"
            return {"type": "clarify", "question": question, "pending": text}
        return {
            "type": "proposal",
            "title": "改成这样？",
            "body": f"{job['name']}\n{schedule['when']}\n{zone}",
            "allow": "改好",
            "deny": "取消",
            "payload": {
                "action": "reschedule",
                "job_id": job["id"],
                "schedule": schedule["schedule"],
                "when": schedule["when"],
                "zone": zone,
            },
        }
    titles = {
        "pause": (f"先停掉「{job['name']}」？", job["when"], "停掉", "留下"),
        "resume": (f"继续「{job['name']}」？", job["when"], "继续", "取消"),
        "remove": (f"取消「{job['name']}」？", "取消后不再提醒。", "取消这条", "留下"),
    }
    title, body, allow, deny = titles[action]
    return {
        "type": "proposal",
        "title": title,
        "body": body,
        "allow": allow,
        "deny": deny,
        "payload": {"action": action, "job_id": job["id"]},
    }


def _match_jobs(text: str, jobs: list[dict]) -> list[dict]:
    found = []
    for job in jobs:
        name = job.get("name") or ""
        phrase = job.get("phrase") or ""
        if len(name) >= 2 and name in text:
            found.append(job)
        elif len(phrase) >= 2 and phrase in text and phrase != name:
            found.append(job)
    if not found:
        return []
    found.sort(key=lambda job: len(job.get("name") or ""), reverse=True)
    longest = found[0]["name"]
    extra = [job for job in found[1:] if job["name"] not in longest]
    return found if extra else [found[0]]


def _intent(text: str) -> tuple[str, str]:
    taskish = any(word in text for word in ("看看", "查一下", "查", "告诉我", "跟我说", "天气"))
    if "提醒我" in text and not taskish:
        return "remind", _subject(text.split("提醒我", 1)[1])
    for marker in ("跟我说", "告诉我", "看看", "查一下", "查"):
        if marker in text:
            return "task", _subject(text.split(marker, 1)[1])
    if "提醒我" in text:
        return "task", _subject(text.split("提醒我", 1)[1])
    if re.search(r"闹钟|叫我", text) and not taskish:
        spoken = re.sub(r"定一个|定个|设一个|设个|来一个|帮我|给我|闹钟|叫我|的", " ", _subject(text))
        return "remind", " ".join(spoken.split()) or "闹钟"
    return "task", ""


def _subject(text: str) -> str:
    cleaned = re.sub(
        r"每个工作日|工作日|每天|每日|"
        r"每(?:个)?(?:下个?|这个?)?(?:周|星期|礼拜)[0-7一二三四五六日天]?|"
        r"(?:下个?|这个?)?(?:周|星期|礼拜)[0-7一二三四五六日天几]|"
        r"下个月|下月|这个月|几月|"
        rf"(?:\d{{1,2}}|十一|十二|十|[一二三四五六七八九])月(?:\d{{1,2}}|{_CN_INT})?[号日]?|"
        rf"(?:\d{{1,2}}|{_CN_INT})[号日]|几[号日]|"
        r"(?:凌晨|早上|上午|中午|下午|晚上|傍晚)|"
        r"(?:十二|十一|十|[零一二两三四五六七八九]|\d{1,2})[点时](?:半|一刻|三刻)?|"
        r"\d{1,2}[:：]\d{2}|"
        rf"每(?:隔)?(?:\d+|{_CN_INT})?个?小时|每(?:\d+|{_CN_INT})分钟|"
        rf"(?:\d+|半|{_CN_INT})个?小时后|(?:\d+|半|{_CN_INT})分钟后|"
        r"只提醒三次|提醒三次|共三次|提醒我|请|吧|啊",
        " ",
        text,
    )
    return " ".join(cleaned.split())


def _parse_when(text: str, now: datetime) -> Optional[dict]:
    if "每个工作日" in text or ("工作日" in text and "每" in text):
        clock = _clock(text)
        if clock == "need-hour":
            return {"clarify": "几点？"}
        if clock in {None, "ambiguous", "bad"}:
            return {"clarify": "早上还是晚上？"} if clock == "ambiguous" else {"clarify": "几点？"}
        hour, minute = clock
        return {
            "schedule": f"{minute} {hour} * * 1-5",
            "when": f"每个工作日 {_hm(hour, minute)}",
        }
    if "每天" in text or "每日" in text:
        clock = _clock(text)
        if not isinstance(clock, tuple):
            return {"clarify": "几点？"} if clock != "ambiguous" else {"clarify": "早上还是晚上？"}
        hour, minute = clock
        return {"schedule": f"{minute} {hour} * * *", "when": f"每天 {_hm(hour, minute)}"}
    dated = _mentioned_date(text)
    if dated == "which-day":
        return {"clarify": "几号？"}
    if dated == "which-month":
        return {"clarify": "几月？"}
    if dated == "many":
        return {"clarify": "哪一天？"}
    weekday = _mentioned_weekday(text)
    if weekday == "which":
        return {"clarify": "星期几？"}
    if weekday == "many":
        return {"clarify": "哪一天？"}
    if isinstance(dated, dict) and not (isinstance(weekday, dict) and weekday["every"]):
        clock = _require_clock(text)
        if "clarify" in clock:
            return clock
        hour, minute = clock["clock"]
        placed = _place_date(now, dated, hour, minute)
        if "clarify" in placed:
            return placed
        when = placed["when"]
        return {
            "schedule": when.isoformat(),
            "when": f"{when.month}月{when.day}日 {_hm(hour, minute)}，只一次",
        }
    if isinstance(weekday, dict) and weekday["every"]:
        clock = _require_clock(text)
        if "clarify" in clock:
            return clock
        hour, minute = clock["clock"]
        return {
            "schedule": f"{minute} {hour} * * {weekday['dow']}",
            "when": f"每周{weekday['label']} {_hm(hour, minute)}",
        }
    if isinstance(weekday, dict):
        clock = _require_clock(text)
        if "clarify" in clock:
            return clock
        hour, minute = clock["clock"]
        when = _on_weekday(now, weekday["dow"], hour, minute, week=weekday["week"])
        if when <= now:
            return {"clarify": "这个时间已经过了"}
        return {
            "schedule": when.isoformat(),
            "when": f"{weekday['label']} {when.month}月{when.day}日 {_hm(hour, minute)}，只一次",
        }
    every_minutes = re.search(rf"每(\d+|{_CN_INT})分钟", text)
    if every_minutes:
        count = _cn_int(every_minutes.group(1))
        if count is None or count <= 0:
            return {"clarify": "每隔多久？"}
        return {"schedule": f"{count}m", "when": f"每 {count} 分钟"}
    every_hours = re.search(rf"每(?:隔)?(\d+|{_CN_INT})?个?小时(?!后)", text)
    if every_hours and "每天" not in text:
        count = _cn_int(every_hours.group(1) or "一")
        if count is None or count <= 0:
            return {"clarify": "每隔多久？"}
        return {"schedule": f"every {count}h", "when": f"每 {count} 小时"}
    after = re.search(rf"(\d+|半|{_CN_INT})个?小时后|(\d+|半|{_CN_INT})分钟后", text)
    if after:
        if after.group(1):
            token = after.group(1)
            if token == "半":
                return {"schedule": "in 30m", "when": "30 分钟后，只一次"}
            count = _cn_int(token)
            if count is None or count <= 0:
                return {"clarify": "几点？"}
            return {"schedule": f"in {count}h", "when": f"{count} 小时后，只一次"}
        count = 30 if after.group(2) == "半" else _cn_int(after.group(2))
        if count is None or count <= 0:
            return {"clarify": "几点？"}
        return {"schedule": f"in {count}m", "when": f"{count} 分钟后，只一次"}
    clock = _clock(text)
    if clock == "need-hour":
        return {"clarify": "几点？"}
    if clock == "ambiguous":
        return {"clarify": "早上还是晚上？"}
    if clock == "bad":
        return {"clarify": "几点？"}
    if clock is None:
        return None
    hour, minute = clock
    day = 0
    if "后天" in text:
        day = 2
    elif "明天" in text:
        day = 1
    elif "今天" in text:
        day = 0
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=day)
    if day == 0 and "今天" not in text and "明天" not in text and "后天" not in text and when <= now:
        when += timedelta(days=1)
        day = 1
    if when <= now:
        return {"clarify": "这个时间已经过了"}
    prefix = {0: "今天", 1: "明天", 2: "后天"}.get(day, f"{when.month}月{when.day}日")
    if day > 2:
        prefix = f"{when.month}月{when.day}日"
    return {"schedule": when.isoformat(), "when": f"{prefix} {_hm(hour, minute)}，只一次"}


def _cn_int(token: str) -> Optional[int]:
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if token in _CN_DIGIT:
        return _CN_DIGIT[token]
    if token == "十":
        return 10
    if "十" not in token:
        return None
    left, right = token.split("十", 1)
    if left and left not in _CN_DIGIT:
        return None
    if right and right not in _CN_DIGIT:
        return None
    tens = _CN_DIGIT[left] if left else 1
    ones = _CN_DIGIT[right] if right else 0
    return tens * 10 + ones


def _dow(token: str) -> Optional[int]:
    if token in _WEEKDAY:
        return _WEEKDAY[token]
    if token.isdigit() and token in "123456":
        return int(token)
    return None


def _require_clock(text: str) -> dict:
    clock = _clock(text)
    if isinstance(clock, tuple):
        return {"clock": clock}
    if clock == "ambiguous":
        return {"clarify": "早上还是晚上？"}
    return {"clarify": "几点？"}


def _mentioned_weekday(text: str):
    if re.search(r"(?:周|星期|礼拜)几", text):
        return "which"
    found = list(re.finditer(
        r"(每(?:个)?)?(下个?|这个?)?(?:周|星期|礼拜)([0-7一二三四五六日天])",
        text,
    ))
    if not found:
        if re.search(r"每(?:个)?(?:周|星期|礼拜)", text):
            return "which"
        return None
    if len(found) > 1:
        return "many"
    every, when, day = found[0].groups()
    dow = _dow(day)
    if dow is None:
        return "which"
    if every:
        week = "every"
    elif when and when.startswith("下"):
        week = "next"
    elif when and when.startswith("这"):
        week = "this"
    else:
        week = "soon"
    name = "日" if dow == 0 else "一二三四五六"[dow - 1]
    label = {"next": f"下周{name}", "this": f"这周{name}", "soon": f"周{name}", "every": f"周{name}"}[week]
    return {"dow": dow, "name": name, "label": label, "every": week == "every", "week": week}


def _on_weekday(now: datetime, dow: int, hour: int, minute: int, *, week: str) -> datetime:
    """The Monday-start week used in everyday Chinese: 下周一 is Monday of the next week."""
    python_dow = 6 if dow == 0 else dow - 1

    def at(day: datetime) -> datetime:
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if week == "next":
        days_to_next_monday = (7 - now.weekday()) % 7 or 7
        monday = at(now + timedelta(days=days_to_next_monday))
        return monday + timedelta(days=python_dow)
    if week == "this":
        monday = at(now - timedelta(days=now.weekday()))
        return monday + timedelta(days=python_dow)
    when = at(now + timedelta(days=(python_dow - now.weekday()) % 7))
    if when <= now:
        when += timedelta(days=7)
    return when


def _mentioned_date(text: str):
    if re.search(r"几[号日]", text):
        return "which-day"
    if "几月" in text:
        return "which-month"
    found = list(re.finditer(
        rf"(?:(\d{{1,2}}|十一|十二|十|[一二三四五六七八九])月)?(\d{{1,2}}|{_CN_INT})[号日]",
        text,
    ))
    if len(found) > 1:
        return "many"
    if not found:
        return None
    month_token, day_token = found[0].groups()
    day = _cn_int(day_token)
    month = _cn_int(month_token) if month_token else None
    if day is None:
        return "which-day"
    if month_token and month is None:
        return "which-month"
    return {
        "month": month,
        "day": day,
        "next_month": month is None and ("下个月" in text or "下月" in text),
        "this_month": month is None and "这个月" in text,
    }


def _place_date(now: datetime, dated: dict, hour: int, minute: int) -> dict:
    day = dated["day"]
    month = dated["month"]
    if not 1 <= day <= 31:
        return {"clarify": "几号？"}
    if month is not None and not 1 <= month <= 12:
        return {"clarify": "几月？"}
    year = now.year
    if month is None:
        month = now.month + (1 if dated["next_month"] else 0)
        if month > 12:
            year += 1
            month -= 12

    def build(year: int, month: int):
        try:
            return now.replace(year=year, month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None

    when = build(year, month)
    if when is None:
        return {"clarify": "这个日子没有"}
    if dated["month"] is None and not dated["this_month"] and not dated["next_month"] and when <= now:
        month += 1
        if month > 12:
            year += 1
            month = 1
        when = build(year, month)
        if when is None:
            return {"clarify": "这个日子没有"}
    elif dated["month"] is not None and when <= now:
        when = build(year + 1, month)
        if when is None:
            return {"clarify": "这个日子没有"}
    if when is None or when <= now:
        return {"clarify": "这个时间已经过了"}
    return {"when": when}


def _clock(text: str):
    parts = [part for part in ("凌晨", "早上", "上午", "中午", "下午", "晚上", "傍晚") if part in text]
    if len(parts) > 1:
        return "ambiguous"
    part = parts[0] if parts else None
    matched = re.search(r"(\d{1,2})[:：](\d{2})", text)
    if matched:
        hour, minute = int(matched.group(1)), int(matched.group(2))
    else:
        matched = re.search(
            r"(十二|十一|十|零|一|二|两|三|四|五|六|七|八|九|\d{1,2})[点时](半|一刻|三刻)?",
            text,
        )
        if not matched:
            return "need-hour" if part else None
        token = matched.group(1)
        hour = int(token) if token.isdigit() else dict(_HOUR_WORDS)[token]
        minute = {"半": 30, "一刻": 15, "三刻": 45}.get(matched.group(2) or "", 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return "bad"
    if part is None and 1 <= hour <= 12:
        return "ambiguous"
    return _apply_daypart(hour, minute, part)


def _apply_daypart(hour: int, minute: int, part: Optional[str]) -> tuple[int, int]:
    if part is None:
        return hour, minute
    if part == "中午":
        return (hour + 12, minute) if hour < 11 else (hour, minute)
    if part == "晚上" and hour == 12:
        return 0, minute
    if part in {"下午", "晚上", "傍晚"} and hour < 12:
        return hour + 12, minute
    if part in {"凌晨", "早上", "上午"} and hour == 12:
        return 0, minute
    return hour, minute


def _count(token: str) -> int:
    known = {"半": 30, "两": 2, "一": 1, "二": 2}
    if token in known:
        return known[token]
    return int(token)


def _hm(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def _peek(proposal_id: str) -> dict:
    with _db() as database:
        row = database.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None or row["consumed"] or row["expires"] < time.time():
            raise ValueError("已经处理过了" if row is not None and row["consumed"] else "这张卡片过期了")
        return json.loads(row["payload"])


def _consume(proposal_id: str) -> dict:
    with _db() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None or row["consumed"] or row["expires"] < time.time():
            raise ValueError("已经处理过了" if row is not None and row["consumed"] else "这张卡片过期了")
        updated = database.execute(
            "UPDATE proposals SET consumed = 1 WHERE id = ? AND consumed = 0",
            (proposal_id,),
        )
        if updated.rowcount != 1:
            raise ValueError("已经处理过了")
        return json.loads(row["payload"])


def _require_room() -> None:
    if len(_jobs()) >= schedule_limit():
        raise ValueError("先停掉或取消一条，才能再记新的")


def _create_job(payload: dict) -> dict:
    from cron.jobs import create_job

    kind = payload["kind"]
    phrase = str(payload.get("phrase") or "")
    task = str(payload.get("task") or "")
    prompt = phrase if kind == "remind" else (
        "到点了。请完成下面这件事，用简短的中文直接说出结果。不要提问，不要等待确认。\n" + task
    )
    origin = {
        "platform": "pocket",
        "chat_id": str(payload.get("profile") or "pocket"),
        "pocket_kind": kind,
        "pocket_when": payload.get("when") or "",
        "pocket_phrase": phrase,
        "pocket_zone": payload.get("zone") or "",
    }
    return create_job(
        prompt,
        str(payload["schedule"]),
        name=str(payload.get("name") or "日程"),
        repeat=payload.get("repeat"),
        deliver="pocket",
        origin=origin,
    )


def _find(job_id: str) -> Optional[dict]:
    return next((view for view in _views() if view["id"] == job_id), None)


def _pause(job_id: str) -> None:
    from cron.jobs import pause_job

    if pause_job(job_id, "user") is None:
        raise ValueError("没有找到这条日程")


def _resume(job: dict) -> str:
    from cron.jobs import _instant_at_or_before, _parse_aware, compute_next_run, update_job
    import hermes_time

    nxt = compute_next_run(job["schedule"])
    when = _parse_aware(nxt) if nxt else None
    if nxt is None or when is None or _instant_at_or_before(when, hermes_time.now()):
        raise ValueError("这个时间已经过了")
    update_job(job["id"], {
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "next_run_at": nxt,
    })
    return _next_label(nxt)


def _remove(job_id: str) -> None:
    from cron.jobs import remove_job

    remove_job(job_id)
    with _db() as database:
        database.execute("DELETE FROM inbox WHERE job_id = ? AND heard_at IS NULL", (job_id,))


def _reschedule(job: dict, payload: dict) -> None:
    from cron.jobs import update_job

    origin = dict(job.get("origin") or {})
    origin["pocket_when"] = payload.get("when") or origin.get("pocket_when")
    origin["pocket_zone"] = payload.get("zone") or origin.get("pocket_zone")
    update_job(job["id"], {"schedule": payload["schedule"], "origin": origin})


def _latest_by_job() -> dict[str, dict]:
    with _db() as database:
        rows = database.execute(
            "SELECT job_id, text, ok, created_at FROM inbox ORDER BY created_at DESC"
        )
        found: dict[str, dict] = {}
        for row in rows:
            if row["job_id"] not in found:
                found[row["job_id"]] = {"text": row["text"], "ok": bool(row["ok"])}
        return found


def _failure_already_noted(job_id: str) -> bool:
    with _db() as database:
        row = database.execute(
            "SELECT ok FROM inbox WHERE job_id = ? ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    return bool(row and not row["ok"])


def _list_speech(views: list[dict]) -> str:
    current = [view for view in views if not view.get("expired")]
    if not current:
        return "待办都过期了。" if views else "还没有日程。"
    head = current[:3]
    bits = []
    for view in head:
        bit = f"{view['when']} {view['name']}"
        if view["paused"]:
            bit += "，暂停中"
        bits.append(bit)
    more = f"。还有 {len(current) - len(head)} 条" if len(current) > len(head) else ""
    return f"你有 {len(current)} 条。{'。'.join(bits)}{more}。"


def _next_label(value: Optional[str]) -> str:
    if not value:
        return ""
    import hermes_time

    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return ""
    now = hermes_time.now()
    if when.tzinfo is None:
        when = when.replace(tzinfo=now.tzinfo)
    when = when.astimezone(now.tzinfo)
    day = (when.date() - now.date()).days
    clock = when.strftime("%H:%M")
    if day == 0:
        return f"今天 {clock}"
    if day == 1:
        return f"明天 {clock}"
    return f"{when.month}月{when.day}日 {clock}"

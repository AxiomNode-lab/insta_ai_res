import copy
import json
import re
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.all_models import Setting

OWNER_TEXTS_PREFIX = "owner_texts"
COMMENT_DM_RULES_PREFIX = "comment_dm_rules"
BUSINESS_HOURS_PREFIX = "business_hours"

OWNER_TEXT_DEFAULTS = {
    "welcome_text": "👋 أهلاً بك! أنا مساعد المتجر، كيف يمكنني خدمتك؟",
    "fallback_text": "تم استلام رسالتك وسيتم الرد عليك قريباً من فريق المتجر ⏳",
    "soft_welcome_text": "أهلاً بك 👋",
}

BUSINESS_HOURS_DEFAULTS = {
    "enabled": False,
    "timezone": "UTC",
    "days": [0, 1, 2, 3, 4],
    "open": "09:00",
    "close": "17:00",
    "after_hours_text": "شكراً لرسالتك. نحن خارج ساعات العمل الآن وسيرد عليك الفريق عند عودته.",
}

_CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[\u064B-\u065F\u0640]", "", text.strip().lower())
    text = re.sub(r"[إأآ]", "ا", text)
    text = re.sub(r"ى", "ي", text)
    text = re.sub(r"ة", "ه", text)
    return text


def _account_key(prefix: str, account_id: int) -> str:
    return f"{prefix}:{account_id}"


async def _get_json_setting(session: AsyncSession, key: str, default: Any) -> Any:
    stmt = select(Setting).where(Setting.key == key)
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if not row or not row.value:
        return copy.deepcopy(default)
    try:
        return json.loads(row.value)
    except json.JSONDecodeError:
        return copy.deepcopy(default)


async def _set_json_setting(session: AsyncSession, key: str, value: Any) -> None:
    stmt = select(Setting).where(Setting.key == key)
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    encoded = json.dumps(value, ensure_ascii=False)
    if row:
        row.value = encoded
    else:
        session.add(Setting(key=key, value=encoded))


def _sanitize_owner_texts(raw: dict[str, Any]) -> dict[str, str]:
    data = dict(OWNER_TEXT_DEFAULTS)
    if not isinstance(raw, dict):
        return data

    for key in OWNER_TEXT_DEFAULTS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            data[key] = value.strip()
    return data


def _sanitize_comment_rules(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []

    seen: set[str] = set()
    rules: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        keyword = item.get("keyword")
        response = item.get("response")
        if not isinstance(keyword, str) or not isinstance(response, str):
            continue
        keyword = keyword.strip()
        response = response.strip()
        if not keyword or not response:
            continue

        normalized = normalize_text(keyword)
        if not normalized or normalized in seen:
            continue

        seen.add(normalized)
        rules.append({"keyword": keyword, "response": response})

    return rules


def sanitize_business_hours(raw: Any) -> dict[str, Any]:
    data = copy.deepcopy(BUSINESS_HOURS_DEFAULTS)
    if not isinstance(raw, dict):
        return data

    data["enabled"] = raw.get("enabled") is True

    timezone_name = raw.get("timezone")
    if isinstance(timezone_name, str):
        try:
            ZoneInfo(timezone_name)
            data["timezone"] = timezone_name
        except ZoneInfoNotFoundError:
            pass

    days = raw.get("days")
    if isinstance(days, list):
        valid_days = sorted({day for day in days if isinstance(day, int) and 0 <= day <= 6})
        if valid_days:
            data["days"] = valid_days

    for key in ("open", "close"):
        value = raw.get(key)
        if isinstance(value, str) and _CLOCK_RE.fullmatch(value):
            data[key] = value

    after_hours_text = raw.get("after_hours_text")
    if isinstance(after_hours_text, str) and after_hours_text.strip():
        data["after_hours_text"] = after_hours_text.strip()[:1000]

    return data


def is_business_open(config: dict[str, Any], now: datetime | None = None) -> bool:
    config = sanitize_business_hours(config)
    if not config["enabled"]:
        return True

    zone = ZoneInfo(config["timezone"])
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(zone)

    opens = time.fromisoformat(config["open"])
    closes = time.fromisoformat(config["close"])
    current_time = local.time().replace(tzinfo=None)
    days = set(config["days"])

    if opens < closes:
        return local.weekday() in days and opens <= current_time < closes
    if opens > closes:
        return (
            (local.weekday() in days and current_time >= opens)
            or ((local.weekday() - 1) % 7 in days and current_time < closes)
        )
    return local.weekday() in days


async def get_owner_texts(session: AsyncSession, account_id: int) -> dict[str, str]:
    key = _account_key(OWNER_TEXTS_PREFIX, account_id)
    raw = await _get_json_setting(session, key, {})
    return _sanitize_owner_texts(raw)


async def set_owner_text(session: AsyncSession, account_id: int, text_key: str, value: str) -> None:
    if text_key not in OWNER_TEXT_DEFAULTS:
        raise ValueError(f"Unsupported owner text key: {text_key}")
    if not value or not value.strip():
        raise ValueError("Owner text value cannot be empty")

    key = _account_key(OWNER_TEXTS_PREFIX, account_id)
    raw = await _get_json_setting(session, key, {})
    if not isinstance(raw, dict):
        raw = {}
    raw[text_key] = value.strip()
    await _set_json_setting(session, key, raw)


async def reset_owner_text(session: AsyncSession, account_id: int, text_key: str) -> None:
    if text_key not in OWNER_TEXT_DEFAULTS:
        raise ValueError(f"Unsupported owner text key: {text_key}")

    key = _account_key(OWNER_TEXTS_PREFIX, account_id)
    raw = await _get_json_setting(session, key, {})
    if not isinstance(raw, dict):
        raw = {}
    raw.pop(text_key, None)
    await _set_json_setting(session, key, raw)


async def get_comment_dm_rules(session: AsyncSession, account_id: int) -> list[dict[str, str]]:
    key = _account_key(COMMENT_DM_RULES_PREFIX, account_id)
    raw = await _get_json_setting(session, key, [])
    return _sanitize_comment_rules(raw)


async def get_business_hours(session: AsyncSession, account_id: int) -> dict[str, Any]:
    key = _account_key(BUSINESS_HOURS_PREFIX, account_id)
    raw = await _get_json_setting(session, key, {})
    return sanitize_business_hours(raw)


async def set_business_hours(
    session: AsyncSession,
    account_id: int,
    *,
    timezone_name: str,
    days: list[int],
    opens: str,
    closes: str,
) -> dict[str, Any]:
    current = await get_business_hours(session, account_id)
    candidate = {
        **current,
        "timezone": timezone_name,
        "days": days,
        "open": opens,
        "close": closes,
    }
    validated = sanitize_business_hours(candidate)
    if validated["timezone"] != timezone_name:
        raise ValueError("Unknown IANA timezone")
    if validated["days"] != sorted(set(days)) or not days:
        raise ValueError("Days must contain values from 0 to 6")
    if validated["open"] != opens or validated["close"] != closes:
        raise ValueError("Times must use HH:MM in 24-hour format")
    await _set_json_setting(session, _account_key(BUSINESS_HOURS_PREFIX, account_id), validated)
    return validated


async def set_business_hours_enabled(
    session: AsyncSession, account_id: int, enabled: bool
) -> dict[str, Any]:
    current = await get_business_hours(session, account_id)
    current["enabled"] = bool(enabled)
    await _set_json_setting(session, _account_key(BUSINESS_HOURS_PREFIX, account_id), current)
    return current


async def set_after_hours_text(
    session: AsyncSession, account_id: int, value: str
) -> dict[str, Any]:
    value = (value or "").strip()
    if not value:
        raise ValueError("After-hours text cannot be empty")
    current = await get_business_hours(session, account_id)
    current["after_hours_text"] = value[:1000]
    await _set_json_setting(session, _account_key(BUSINESS_HOURS_PREFIX, account_id), current)
    return current


async def upsert_comment_dm_rule(
    session: AsyncSession,
    account_id: int,
    keyword: str,
    response: str,
) -> bool:
    keyword = (keyword or "").strip()
    response = (response or "").strip()
    if not keyword or not response:
        raise ValueError("Keyword and response are required")

    key = _account_key(COMMENT_DM_RULES_PREFIX, account_id)
    rules = await get_comment_dm_rules(session, account_id)
    normalized = normalize_text(keyword)
    inserted = True

    for rule in rules:
        if normalize_text(rule["keyword"]) == normalized:
            rule["keyword"] = keyword
            rule["response"] = response
            inserted = False
            break
    else:
        rules.append({"keyword": keyword, "response": response})

    await _set_json_setting(session, key, rules)
    return inserted


async def delete_comment_dm_rule(session: AsyncSession, account_id: int, keyword: str) -> bool:
    normalized = normalize_text(keyword)
    if not normalized:
        return False

    key = _account_key(COMMENT_DM_RULES_PREFIX, account_id)
    rules = await get_comment_dm_rules(session, account_id)
    filtered = [rule for rule in rules if normalize_text(rule["keyword"]) != normalized]
    deleted = len(filtered) != len(rules)

    if deleted:
        await _set_json_setting(session, key, filtered)
    return deleted


def find_comment_dm_match(
    rules: list[dict[str, str]],
    comment_text: str,
) -> tuple[str, str] | tuple[None, None]:
    normalized_comment = normalize_text(comment_text)
    if not normalized_comment:
        return None, None

    for rule in rules:
        keyword = rule["keyword"]
        response = rule["response"]
        normalized_keyword = normalize_text(keyword)
        if normalized_keyword and normalized_keyword in normalized_comment:
            return keyword, response

    return None, None

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.core.redis_utils import QueueBackpressure, enqueue_after_hours_notice
from app.services.account_settings import is_business_open, sanitize_business_hours
from app.services.instagram_service import (
    DeliveryPolicyBlocked,
    _process_comment_change,
    _process_single_event,
    _tenant_live_chat_recipients,
    build_instagram_message_payload,
    extract_event_text,
    notify_live_chat,
    send_instagram_message_api,
)


def test_private_reply_payload_targets_comment_id():
    payload = build_instagram_message_payload("comment-123", "تفاصيل المنتج", "comment")

    assert payload["recipient"] == {"comment_id": "comment-123"}
    assert payload["message"] == {"text": "تفاصيل المنتج"}


def test_regular_message_payload_targets_user_id():
    payload = build_instagram_message_payload("user-123", "مرحباً")

    assert payload["recipient"] == {"id": "user-123"}


@pytest.mark.asyncio
async def test_live_chat_recipients_are_scoped_to_the_tenant():
    result = MagicMock()
    result.scalars.return_value.all.return_value = [101, 102, 101]
    session = AsyncMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.instagram_service.AsyncSessionLocal", return_value=context):
        recipients = await _tenant_live_chat_recipients(7)

    assert recipients == [101, 102]
    statement = session.execute.await_args.args[0]
    assert statement.compile().params == {"account_id_1": 7}


@pytest.mark.asyncio
async def test_live_chat_does_not_broadcast_customer_data_to_global_admins():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.session.close = AsyncMock()

    with (
        patch(
            "app.services.instagram_service._tenant_live_chat_recipients",
            AsyncMock(return_value=[101, 102]),
        ),
        patch("app.services.instagram_service.Bot", return_value=bot),
        patch.object(settings, "ADMIN_IDS", [999]),
    ):
        await notify_live_chat(7, "synthetic-user", "hello", "Synthetic")

    assert {call.kwargs["chat_id"] for call in bot.send_message.await_args_list} == {
        101,
        102,
    }
    bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_after_hours_cooldown_and_queue_insert_are_one_redis_operation():
    redis = AsyncMock()
    redis.eval.return_value = 1
    with patch("app.core.redis_utils.get_redis_client", AsyncMock(return_value=redis)):
        queued = await enqueue_after_hours_notice("user-123", "closed", 7, delay=2)

    assert queued is True
    assert redis.eval.await_count == 1
    args = redis.eval.await_args.args
    assert args[2] == "after_hours_notice:7:user-123"
    assert args[3] == "queue:7"


@pytest.mark.asyncio
async def test_after_hours_atomic_enqueue_reports_backpressure():
    redis = AsyncMock()
    redis.eval.return_value = -1
    with patch("app.core.redis_utils.get_redis_client", AsyncMock(return_value=redis)):
        with pytest.raises(QueueBackpressure):
            await enqueue_after_hours_notice("user-123", "closed", 7)


@pytest.mark.asyncio
async def test_existing_after_hours_cooldown_is_a_successful_noop():
    redis = AsyncMock()
    redis.eval.return_value = 0
    with patch("app.core.redis_utils.get_redis_client", AsyncMock(return_value=redis)):
        queued = await enqueue_after_hours_notice("user-123", "closed", 7)

    assert queued is False


@pytest.mark.asyncio
async def test_regular_dm_remains_blocked_outside_24_hour_window():
    with patch(
        "app.services.instagram_service.is_within_24h_window",
        AsyncMock(return_value=False),
    ):
        with pytest.raises(DeliveryPolicyBlocked):
            await send_instagram_message_api(7, "user-123", "hello", "token")


@pytest.mark.asyncio
async def test_private_reply_uses_comment_authorization_not_dm_window():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"recipient_id": "private-reply"}
    client = AsyncMock()
    client.post.return_value = response
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    window_check = AsyncMock(return_value=False)

    with (
        patch("app.services.instagram_service.is_within_24h_window", window_check),
        patch("app.services.instagram_service.httpx.AsyncClient", return_value=context),
        patch("app.services.instagram_service.generate_appsecret_proof", return_value="proof"),
        patch("app.core.redis_utils.track_outgoing_message", AsyncMock()),
    ):
        result = await send_instagram_message_api(
            7, "comment-123", "details", "token", recipient_type="comment"
        )

    assert result == {"recipient_id": "private-reply"}
    window_check.assert_not_awaited()
    assert client.post.await_args.kwargs["json"]["recipient"] == {
        "comment_id": "comment-123"
    }


@pytest.mark.asyncio
async def test_comment_rule_queues_one_meta_private_reply_to_comment():
    session = SimpleNamespace(add=lambda _value: None, commit=AsyncMock())
    account = SimpleNamespace(id=7)
    change = {
        "field": "comments",
        "value": {
            "id": "comment-123",
            "text": "ارسل السعر",
            "from": {"id": "user-456"},
            "media": {"media_product_type": "REELS"},
        },
    }

    with (
        patch(
            "app.services.instagram_service.get_comment_dm_rules",
            AsyncMock(return_value=[{"keyword": "السعر", "response": "السعر 10"}]),
        ),
        patch(
            "app.services.instagram_service.enqueue_private_reply", new_callable=AsyncMock
        ) as enqueue,
        patch(
            "app.services.instagram_service.get_or_create_user",
            AsyncMock(return_value=SimpleNamespace(id=99)),
        ),
        patch("app.services.instagram_service.random.uniform", return_value=3),
    ):
        await _process_comment_change(change, account, session)

    enqueue.assert_awaited_once_with("comment-123", "السعر 10", 7, delay=3)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_after_hours_path_applies_user_limit_before_storage_or_notification():
    session = SimpleNamespace(add=MagicMock(), commit=AsyncMock())
    account = SimpleNamespace(id=7)
    user = SimpleNamespace(id=99, last_reply_status=None, full_name=None)
    event = {
        "sender": {"id": "user-123"},
        "recipient": {"id": "page-7"},
        "timestamp": 1_777_000_000,
        "message": {"mid": "mid-1", "text": "hello there"},
    }
    notify = AsyncMock()

    with (
        patch(
            "app.services.instagram_service.check_follow_up_cooldown",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.services.instagram_service.is_human_takeover_active",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.services.instagram_service.get_business_hours",
            AsyncMock(return_value={"enabled": True}),
        ),
        patch("app.services.instagram_service.is_business_open", return_value=False),
        patch("app.services.instagram_service.is_rate_limited", AsyncMock(return_value=True)),
        patch(
            "app.services.instagram_service.check_account_limits",
            AsyncMock(),
        ) as account_limit,
        patch(
            "app.services.instagram_service.get_or_create_user",
            AsyncMock(return_value=user),
        ),
        patch("app.services.instagram_service.notify_live_chat", notify),
    ):
        await _process_single_event(event, account, session)

    assert user.last_reply_status == "RATE_LIMITED"
    account_limit.assert_not_awaited()
    notify.assert_not_awaited()
    session.commit.assert_awaited_once()


@pytest.mark.parametrize(
    "event,expected",
    [
        ({"message": {"text": " shipping "}}, "shipping"),
        ({"message": {"quick_reply": {"payload": "TRACK_ORDER"}}}, "TRACK_ORDER"),
        ({"postback": {"title": "Track order", "payload": "TRACK_ORDER"}}, "Track order"),
        ({"postback": {"payload": "GET_STARTED"}}, "GET_STARTED"),
    ],
)
def test_extracts_meta_message_and_postback_text(event, expected):
    assert extract_event_text(event) == expected


def test_business_hours_for_normal_weekday_schedule():
    config = {
        "enabled": True,
        "timezone": "UTC",
        "days": [0, 1, 2, 3, 4],
        "open": "09:00",
        "close": "17:00",
    }

    assert is_business_open(config, datetime(2026, 8, 24, 10, tzinfo=timezone.utc))
    assert not is_business_open(config, datetime(2026, 8, 24, 18, tzinfo=timezone.utc))
    assert not is_business_open(config, datetime(2026, 8, 23, 10, tzinfo=timezone.utc))


def test_business_hours_support_overnight_shifts():
    config = {
        "enabled": True,
        "timezone": "UTC",
        "days": [0],
        "open": "22:00",
        "close": "02:00",
    }

    assert is_business_open(config, datetime(2026, 8, 24, 23, tzinfo=timezone.utc))
    assert is_business_open(config, datetime(2026, 8, 25, 1, tzinfo=timezone.utc))
    assert not is_business_open(config, datetime(2026, 8, 25, 3, tzinfo=timezone.utc))


def test_invalid_business_hours_fall_back_to_safe_defaults():
    config = sanitize_business_hours(
        {
            "enabled": "yes",
            "timezone": "Not/A_Zone",
            "days": [9, "1"],
            "open": "99:99",
            "close": "bad",
            "after_hours_text": " ",
        }
    )

    assert config["enabled"] is False
    assert config["timezone"] == "UTC"
    assert config["days"] == [0, 1, 2, 3, 4]
    assert config["open"] == "09:00"
    assert config["close"] == "17:00"

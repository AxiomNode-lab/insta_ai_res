import httpx
import logging
import asyncio
import random
from datetime import datetime
from multiprocessing import Event
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.security import generate_appsecret_proof, decrypt_token
from app.core.redis_utils import (
    is_event_processed, 
    is_rate_limited, 
    check_account_limits,
    is_account_quarantined,
    set_human_takeover, 
    is_human_takeover_active,
    update_last_interaction,
    is_within_24h_window,
    enqueue_message,
    get_next_message_from_queue,
    acquire_queue_lock,
    move_to_dead_letter,
    acquire_conversation_lock,
    release_conversation_lock,
    check_daily_conversation_limit,
    check_reply_repetition,
    set_account_safe_mode,
    is_global_protection_active,
    record_global_behavior,
    record_global_reply_pattern,
    is_app_risk_high,
    track_conversation_depth,
    check_follow_up_cooldown,
    check_conversation_diversity,
    get_account_behavior_state,
    is_platform_behavior_risk,
    is_trust_recovery_mode,
    record_bot_like_start
)
from app.models.all_models import User, Message, MessageDirection, Account, ActivityEvent, LastProcessedEvent, Conversation
from app.services.reply_engine import get_auto_reply, normalize_arabic
from app.services.account_settings import (
    find_comment_dm_match,
    get_comment_dm_rules,
    get_owner_texts,
)
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
import re
from aiogram import Bot
from app.bot.keyboards import InlineKeyboardMarkup, InlineKeyboardButton
import json
import time

logger = logging.getLogger(__name__)

GRAPH_API_URL = "https://graph.facebook.com/v19.0"


def _truncate_log_text(text: str, limit: int = 300) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit - 3]}..."


def _format_http_error(response: httpx.Response) -> str:
    return f"status={response.status_code}, body={_truncate_log_text(response.text)}"

def analyze_reply_risk(text: str, user: User) -> tuple[str, str]:
    """
    Analyzes the reply text for risk indicators.
    Returns: (RiskLevel, Reason)
    """
    normalized = normalize_arabic(text)
    
    # 1. High Risk Indicators (Sales Aggression)
    sales_keywords = ["اشتر", "اطلب الان", "تواصل واتساب", "عرض خاص", "سارع", "لفترة محدودة", "اضغط هنا"]
    if any(kw in normalized for kw in sales_keywords):
        # Only strict if it's the very first message? No, generally avoid aggressive sales in auto-reply.
        # Let's say if msg count < 2
        return "HIGH_RISK", "Aggressive sales language detected"
        
    # 2. Spam Indicators
    if text.count("!") > 3 or text.count("http") > 1:
        return "HIGH_RISK", "Spam format (too many links or symbols)"
        
    if len(text) > 400:
        return "HIGH_RISK", "Message too long (>400 chars)"
        
    return "SAFE", "OK"

# --- Account Identification Middleware ---
async def get_account_by_page_id(session: AsyncSession, page_id: str) -> Account | None:
    stmt = select(Account).where(Account.instagram_page_id == page_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

# --- Live Chat Notification ---
async def notify_live_chat(account_id: int, sender_id: str, text: str, user_name: str = "Unknown"):
    try:
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 رد (Live Chat)", callback_data=f"reply_to:{account_id}:{sender_id}")]
        ])
        msg = f"📩 <b>رسالة جديدة من العميل (Account {account_id})</b>\n\n👤: {user_name} (ID: {sender_id})\n📄: {text}"
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=msg, parse_mode="HTML", reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")
        await bot.session.close()
    except Exception as e:
        logger.error(f"Live Chat Notification Error: {e}")

# --- Worker Logic (Reliability Enhanced) ---
async def process_outgoing_queue(account_id: int = None, stop_event: Event = None, single_pass: bool = False) -> int:
    """
    Background worker that processes the message queue.
    Supports Retry Policy, Dead Letter Queue, and Conversation Locking.
    """
    if not single_pass:
        logger.info(f"Starting Queue Worker for Account {account_id}...")
    
    # If account_id is None, this might be global worker (Phase 3). 
    # But for Phase 4 (Worker Isolation), we expect account_id.
    
    processed_count = 0
    
    while True:
        if stop_event and stop_event.is_set():
            logger.info("Worker stop event received.")
            break
            
        try:
            # --- 1) Global Kill Switch Check ---
            from app.core.redis_utils import is_global_kill_switch_active, is_account_locked, track_outgoing_message
            if await is_global_kill_switch_active():
                if single_pass: return 0
                await asyncio.sleep(5) # Wait while paused
                continue

            # We need to poll ONLY this account's queue.
            # But get_next_message_from_queue polls ALL queues.
            # We need a specific poll function or modify existing.
            # For this context, let's assume get_next_message_from_queue returns ANY message,
            # but in Worker Isolation, we should only process if it matches our account_id (inefficient)
            # OR we implement `get_message_for_account`.
            # Let's implement specific fetch here using redis utils patterns.
            # Or assume we updated `redis_utils` to support fetching specific queue.
            # For simplicity, we manually fetch from `queue:{account_id}` using redis client.
            
            from app.core.redis_utils import get_redis_client
            redis = await get_redis_client()
            queue_key = f"queue:{account_id}"
            data = await redis.lpop(queue_key)
            
            if data:
                processed_count += 1
                msg_data = json.loads(data)
                recipient_id = msg_data["recipient_id"]
                text = msg_data["text"]
                msg_account_id = msg_data.get("account_id")
                delay = msg_data.get("delay", 0)
                
                # Isolation Check
                if msg_account_id and int(msg_account_id) != int(account_id):
                    logger.critical(f"Security Alert: Cross-Account Leak! Expected {account_id}, got {msg_account_id}")
                    continue
                
                # --- 5) Emergency Account Isolation Check ---
                if await is_account_locked(account_id):
                    logger.warning(f"Account {account_id} is in LOCKDOWN. Skipping message to {recipient_id}.")
                    # Should we DLQ it? Or just re-queue?
                    # Re-queueing might cause loop. DLQ is safer for "stuck" state.
                    # Or just drop if it's an emergency? 
                    # Let's push to DLQ to be safe.
                    await move_to_dead_letter(account_id, msg_data, "Account Lockdown Active")
                    continue

                if delay > 0:
                    await asyncio.sleep(delay)
                
                # Fetch Account Token
                async with AsyncSessionLocal() as session:
                    account = await session.get(Account, account_id)
                    if not account:
                        continue
                    # Decrypt Token
                    try:
                        token = decrypt_token(account.id, account.access_token)
                    except Exception as e:
                        logger.error(f"Failed to decrypt token for account {account_id}: {e}")
                        continue
                        
                # Reliability: Conversation Lock
                if not await acquire_conversation_lock(recipient_id):
                    # Locked, push back to queue head or tail?
                    # Pushing back to head preserves order mostly but might loop.
                    # Wait and retry?
                    # Let's push back to head and sleep briefly.
                    await redis.lpush(queue_key, data)
                    await asyncio.sleep(1.0)
                    if single_pass: return processed_count
                    continue

                try:
                    # Retry Policy
                    retries = [0, 5, 30, 120] # Delays
                    success = False
                    
                    for i, delay in enumerate(retries):
                        if i > 0:
                            logger.info(f"Retry {i}/{len(retries)} for {recipient_id} in {delay}s...")
                            await asyncio.sleep(delay)
                        
                        try:
                            # Send
                            await send_instagram_message_api(recipient_id, text, token)
                            success = True
                            break
                        except Exception as e:
                            logger.warning(f"Send failed (Attempt {i+1}): {e}")
                    
                    if not success:
                        logger.error(f"Message to {recipient_id} failed after retries. Moving to DLQ.")
                        await move_to_dead_letter(account_id, msg_data, "Max retries exceeded")
                        
                        # Log Activity (Failed)
                        async with AsyncSessionLocal() as session:
                            event = ActivityEvent(
                                account_id=account_id,
                                event_type="MESSAGE_FAILED",
                                details=f"To: {recipient_id} - Max retries"
                            )
                            session.add(event)
                            await session.commit()
                    else:
                        # Log Activity (Success)
                        async with AsyncSessionLocal() as session:
                            event = ActivityEvent(
                                account_id=account_id,
                                event_type="AUTO_REPLY",
                                details=f"To: {recipient_id}"
                            )
                            session.add(event)
                            await session.commit()
                            
                finally:
                    await release_conversation_lock(recipient_id)
                
                if single_pass:
                    # In single pass (worker pool), we might process just one or a small batch
                    # Returning after 1 message allows fair scheduling
                    return processed_count
                    
                await asyncio.sleep(0.05) 
            else:
                if single_pass:
                    return processed_count
                await asyncio.sleep(1.0)
        except Exception as e:
            logger.error(f"Worker Error: {e}")
            if single_pass: return processed_count
            await asyncio.sleep(1.0)
    return processed_count

async def send_instagram_message_api(recipient_id: str, text: str, access_token: str):
    url = f"{GRAPH_API_URL}/me/messages"
    
    is_within = await is_within_24h_window(recipient_id)
    is_human_mode = await is_human_takeover_active(recipient_id)
    
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
        "messaging_type": "RESPONSE"
    }

    if is_within:
        pass
    elif is_human_mode:
        # 24h Compliance Lock (Even for HUMAN_AGENT)
        # "لا يسمح بالرد حتى لو HUMAN_AGENT إلا بعد رسالة جديدة من المستخدم"
        # This implies we check if the user sent a message recently? 
        # is_within_24h_window means "did user send msg in last 24h".
        # If is_within is False, it means user hasn't sent msg in last 24h.
        # So we strictly block if !is_within.
        logger.warning(f"Blocked message to {recipient_id}: Outside 24h window (Strict Compliance).")
        return
    else:
        logger.warning(f"Blocked message to {recipient_id}: Outside 24h window and not in Human Mode.")
        return

    params = {
        "access_token": access_token,
        "appsecret_proof": generate_appsecret_proof(access_token)
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, params=params, json=payload)
            response.raise_for_status()
            logger.info(f"Message sent to {recipient_id}: {text[:20]}...")
            # --- Track Outgoing for Silence Detection ---
            from app.core.redis_utils import track_outgoing_message
            await track_outgoing_message(1)
            return response.json()
        except httpx.HTTPStatusError as e:
            error_message = _format_http_error(e.response)
            logger.error(f"Failed to send message: {error_message}")
            if e.response.status_code == 429:
                logger.warning("Rate limit hit!")
            raise RuntimeError(error_message) from e
        except Exception as e:
            logger.error(f"Error sending message: {type(e).__name__}")
            raise e

# --- Webhook Logic (Reliability: Replay Recovery) ---

async def process_webhook_payload(payload: dict):
    try:
        for entry in payload.get("entry", []):
            page_id = entry.get("id")
            
            async with AsyncSessionLocal() as session:
                account = await get_account_by_page_id(session, page_id)
                if not account:
                    logger.warning(f"Webhook received for unknown Page ID: {page_id}")
                    continue
                
                # Check Quarantine
                if await is_account_quarantined(account.id):
                    continue

                # Process messaging events for this account
                for event in entry.get("messaging", []):
                    # Reliability: Replay Recovery Check
                    timestamp = event.get("timestamp")
                    if timestamp:
                        # Check last processed timestamp from DB
                        # We use LastProcessedEvent table (Phase 5 requirement 4)
                        # Load last processed
                        # We can cache this in Redis too for speed, but Requirement says "Persistent" (DB).
                        # Let's check DB.
                        
                        stmt = select(LastProcessedEvent).where(LastProcessedEvent.account_id == account.id)
                        res = await session.execute(stmt)
                        last_event = res.scalar_one_or_none()
                        
                        if last_event and int(timestamp) <= int(last_event.last_timestamp):
                            logger.info(f"Skipping replayed event {timestamp} for Account {account.id}")
                            continue
                            
                        # Update Last Timestamp
                        if not last_event:
                            last_event = LastProcessedEvent(account_id=account.id, last_timestamp=str(timestamp))
                            session.add(last_event)
                        else:
                            last_event.last_timestamp = str(timestamp)
                        await session.commit()
                    
                    await process_single_event(event, account, session)
                    
                    # --- Track Incoming for Silence Detection ---
                    from app.core.redis_utils import track_incoming_message
                    # Count user messages only (not echos/deliveries if possible, but payload structure varies)
                    # process_single_event handles logic, but here we count RAW ingress events
                    # We should count only if it's a message?
                    # event.get("message") check
                    if event.get("message") and not event.get("message", {}).get("is_echo"):
                        await track_incoming_message(1)

                # Process comment change events (keyword comment -> private DM)
                for change in entry.get("changes", []):
                    await process_comment_change(change, account, session)
                    
    except Exception as e:
        logger.error(f"Error processing payload: {e}")


async def process_comment_change(change: dict, account: Account, session: AsyncSession):
    if change.get("field") != "comments":
        return

    value = change.get("value") or {}
    comment_id = value.get("id") or value.get("comment_id")
    comment_text = value.get("text")
    commenter = value.get("from") or {}
    commenter_id = commenter.get("id")
    media = value.get("media") or {}
    media_product_type = str(media.get("media_product_type") or media.get("media_type") or "").upper()

    if not comment_id or not comment_text or not commenter_id:
        return

    # User requested keyword private replies for video comments only.
    if media_product_type not in {"VIDEO", "REELS", "IGTV"}:
        return

    dedup_id = f"comment_{account.id}_{comment_id}"
    if await is_event_processed(dedup_id):
        return

    rules = await get_comment_dm_rules(session, account.id)
    matched_keyword, reply_text = find_comment_dm_match(rules, comment_text)
    if not reply_text:
        return

    # Comment itself is treated as a fresh user interaction to pass local 24h window guard.
    await update_last_interaction(commenter_id, timestamp=int(time.time()))
    await enqueue_message(commenter_id, reply_text, account.id, delay=random.uniform(2, 8))

    user = await get_or_create_user(session, commenter_id, account.id, send_welcome=False)
    session.add(
        Message(
            account_id=account.id,
            user_id=user.id,
            content=reply_text,
            direction=MessageDirection.OUTGOING,
            mid=f"comment_dm_{comment_id}",
        )
    )
    session.add(
        ActivityEvent(
            account_id=account.id,
            event_type="COMMENT_DM_TRIGGERED",
            details=f"Keyword: {matched_keyword} | User: {commenter_id} | CommentID: {comment_id}",
        )
    )
    await session.commit()

async def process_single_event(event: dict, account: Account, session: AsyncSession):
    # (Same logic as before, just ensured it's called after Replay Check)
    sender_id = event.get("sender", {}).get("id")
    recipient_id = event.get("recipient", {}).get("id")
    timestamp = event.get("timestamp")
    message = event.get("message", {})
    mid = message.get("mid")
    text = message.get("text")
    is_echo = message.get("is_echo", False)
    
    event_id = f"{timestamp}_{sender_id}_{mid or 'nomid'}"

    if await is_event_processed(event_id):
        return

    if is_echo:
        if recipient_id:
            await set_human_takeover(recipient_id, active=True)
            log = ActivityEvent(account_id=account.id, event_type="HUMAN_INTERVENTION", details=f"Admin replied to {recipient_id}")
            session.add(log)
            await session.commit()
            
            # --- Track Conversation Depth (Bot/Admin Side) ---
            # Need to find conversation ID. 
            # Assuming we can find conversation by user_id
            user = await get_or_create_user(session, recipient_id, account.id, send_welcome=False)
            # Find active conversation
            conv_stmt = select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.last_interaction.desc()).limit(1)
            conv_res = await session.execute(conv_stmt)
            conv = conv_res.scalar_one_or_none()
            if conv:
                await track_conversation_depth(account.id, conv.id, "outgoing")
                
        return

    if not text:
        await update_last_interaction(sender_id, timestamp=int(timestamp) if timestamp else None)
        return
        
    clean_text = re.sub(r'[^\w\s]', '', text).strip()
    if len(clean_text) <= 2:
        await update_last_interaction(sender_id, timestamp=int(timestamp) if timestamp else None)
        return
        
    # --- 5) Follow-Up Cooldown Logic ---
    text_hash = str(hash(clean_text))
    if await check_follow_up_cooldown(sender_id, text_hash):
        # User repeated message < 30s.
        # Don't reply immediately. Just ignore or delay?
        # Prompt says: "Don't repeat reply. Wait 20-40s before next reply."
        # If we return here, we ignore it.
        # Let's log and ignore to avoid loop.
        logger.info(f"Follow-Up Cooldown: Ignoring repeated message from {sender_id}")
        return

    is_human_mode = await is_human_takeover_active(sender_id)
    
    # --- 1) Human Mode Auto-Recovery ---
    if is_human_mode:
        # Check timeout (e.g. 15 mins since last interaction)
        # We rely on last_interaction timestamp in Redis
        # But we need to know if the last interaction was FROM ADMIN or USER?
        # If user keeps talking, we stay in human mode?
        # Requirement: "If admin didn't send message for 15 mins -> cancel"
        # We need "last_admin_reply_time".
        from app.core.redis_utils import get_redis_client
        redis = await get_redis_client()
        last_admin_ts = await redis.get(f"last_admin_reply:{sender_id}")
        
        if last_admin_ts:
            elapsed = int(time.time()) - int(last_admin_ts)
            if elapsed > 900: # 15 mins
                await set_human_takeover(sender_id, False)
                is_human_mode = False
                
                # Update User DB
                user = await get_or_create_user(session, sender_id, account.id)
                user.is_paused = False
                session.add(user)
                
                log = ActivityEvent(account_id=account.id, event_type="HUMAN_TIMEOUT_RETURN", details="Auto-exit human mode")
                session.add(log)
                await session.commit()
                # Proceed to auto-reply logic below (is_human_mode is now False)
        else:
             # No admin reply recorded yet? Maybe just started.
             # Or maybe admin never replied.
             # If user initiated human mode (escalation), and admin never replied...
             # We should probably timeout based on escalation time too.
             # For now, let's assume we stick to human mode until timeout or manual off.
             pass

    if is_human_mode:
        await update_last_interaction(sender_id, timestamp=int(timestamp) if timestamp else None)
        user = await get_or_create_user(session, sender_id, account.id)
        # --- User-Visible Diagnostics ---
        user.last_reply_status = "HUMAN_MODE_ACTIVE"
        session.add(user)
        await session.commit()
        
        await notify_live_chat(account.id, sender_id, text, user.full_name or "User")
        new_msg = Message(
            account_id=account.id,
            user_id=user.id,
            content=text,
            direction=MessageDirection.INCOMING,
            mid=mid
        )
        session.add(new_msg)
        await session.commit()
        return

    # --- 3) Human Escalation Rule ---
    escalation_keywords = ["أريد", "اريد", "احجز", "اطلب", "بدي", "ابغى", "شراء"]
    normalized_text = normalize_arabic(text)
    should_escalate = any(kw in normalized_text for kw in escalation_keywords)
    
    if should_escalate:
        await set_human_takeover(sender_id, active=True)
        await update_last_interaction(sender_id, timestamp=int(timestamp) if timestamp else None)
        
        user = await get_or_create_user(session, sender_id, account.id)
        user.is_paused = True
        user.last_reply_status = "SALES_ESCALATION"
        session.add(user)
        
        log = ActivityEvent(account_id=account.id, event_type="HUMAN_ESCALATION", details=f"Triggered by: {text}")
        session.add(log)
        await session.commit()
        
        await notify_live_chat(account.id, sender_id, text, user.full_name or "User")
        return

    if await is_rate_limited(sender_id):
        user = await get_or_create_user(session, sender_id, account.id)
        user.last_reply_status = "RATE_LIMITED"
        session.add(user)
        await session.commit()
        return

    if not await check_account_limits(account.id):
        log = ActivityEvent(account_id=account.id, event_type="PLAN_LIMIT_REACHED", details="Daily/Hourly limit hit")
        session.add(log)
        
        user = await get_or_create_user(session, sender_id, account.id)
        user.last_reply_status = "PLAN_LIMIT_REACHED"
        session.add(user)
        await session.commit()
        return

    await update_last_interaction(sender_id, timestamp=int(timestamp) if timestamp else None)

    user = await get_or_create_user(session, sender_id, account.id)
    
    # --- Conversation Tracking ---
    # Ensure Conversation exists
    conv_stmt = select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.last_interaction.desc()).limit(1)
    conv_res = await session.execute(conv_stmt)
    conv = conv_res.scalar_one_or_none()
    
    # Create new conversation if old (> 1 hour?) or none
    # Simplified: Just create if none, or update last interaction
    if not conv or (datetime.utcnow() - conv.last_interaction).total_seconds() > 3600:
        conv = Conversation(account_id=account.id, user_id=user.id)
        session.add(conv)
        await session.commit()
        await session.refresh(conv)
    else:
        conv.last_interaction = datetime.utcnow()
        session.add(conv)
        await session.commit()

    # Track Depth (Incoming)
    await track_conversation_depth(account.id, conv.id, "incoming")

    # --- 6) Conversation Diversity Guard ---
    # Check if this user's conversation structure is repetitive
    # We can hash (last_msg -> current_msg)
    # Simplified: Just check repetition of this structure for the account
    # We need a structure hash. Maybe just "msg_in_reply_out" pattern.
    # Implementation: check_conversation_diversity(account.id, "structure_hash")
    # Let's use user_id as salt to check if THIS user is repeating same flow?
    # Or Account level: "Are all users having same conversation?"
    # Prompt: "Monitor per account... if (msg -> reply -> end) repeats often"
    # This logic fits better in background task or reply engine, but here we can check triggers.
    
    # --- 5) Daily Conversation Cap (Gradual Rollout Protection) ---
    # First 24h: 20, 2nd day: 50, 3rd day: 120
    created_date = account.created_at.date()
    days_active = (datetime.utcnow().date() - created_date).days
    
    max_daily_conv = 120
    if days_active <= 1:
        max_daily_conv = 20
    elif days_active <= 2:
        max_daily_conv = 50
        
    if not await check_daily_conversation_limit(account.id, max_limit=max_daily_conv):
        log = ActivityEvent(account_id=account.id, event_type="DAILY_LIMIT_REACHED", details=f"Max {max_daily_conv} exceeded (Day {days_active})")
        session.add(log)
        
        user.last_reply_status = "DAILY_LIMIT_REACHED"
        session.add(user)
        await session.commit()
        return

    new_msg = Message(
        account_id=account.id,
        user_id=user.id,
        content=text,
        direction=MessageDirection.INCOMING,
        mid=mid
    )
    session.add(new_msg)
    await session.commit()
    
    reply_text = await get_auto_reply(session, sender_id, text, account.id)
    
    if reply_text:
        # --- 4) Adaptive Behaviour Controller ---
        behavior_state = await get_account_behavior_state(account.id)
        is_platform_risk = await is_platform_behavior_risk()
        
        adaptive_delay = 0
        
        if behavior_state == "BOT_LIKE_PATTERN" or is_platform_risk:
            # Modify behavior
            adaptive_delay = random.uniform(5, 15) # Add extra delay
            
            # Increase Human Escalation
            if random.random() < 0.2: # 20% chance to force human
                await set_human_takeover(sender_id, True)
                # Notify
                log = ActivityEvent(account_id=account.id, event_type="ADAPTIVE_CONTROL", details="Forced Human Mode due to Bot-Like Behavior")
                session.add(log)
                await session.commit()
                return # Skip auto-reply
                
            # Reduce Auto-Replies
            if random.random() < 0.1: # 10% drop
                 # Just ignore
                 return

        # --- 7) Trust Recovery Mode ---
        if await is_trust_recovery_mode(account.id):
            # Reduce replies 40%
            if random.random() < 0.4:
                return # Drop
            adaptive_delay += random.uniform(10, 30) # Increase delay

        # --- 1) Reply Safety Validation Layer (Pre-Save) ---
        risk_level, risk_reason = analyze_reply_risk(reply_text, user)
        if risk_level == "HIGH_RISK":
            log = ActivityEvent(account_id=account.id, event_type="REPLY_BLOCKED_BY_POLICY", details=f"Blocked: {risk_reason}")
            session.add(log)
            user.last_reply_status = "BLOCKED_HIGH_RISK"
            session.add(user)
            await session.commit()
            return
        elif risk_level == "MEDIUM_RISK":
            # Just log warning but proceed
            log = ActivityEvent(account_id=account.id, event_type="REPLY_WARNING", details=f"Warning: {risk_reason}")
            session.add(log)
            
        # --- 1) Global Trust Protection Layer ---
        # Calculate Risk Score (Async Check)
        is_global_risk = await is_global_protection_active()
        
        # --- 2) No-Sale-Start Policy ---
        msg_count = await session.scalar(select(func.count(Message.id)).where(Message.user_id == user.id))
        if msg_count <= 1: 
            sale_keywords = ["عرض", "خصم", "اطلب", "سجل", "اشتر", "تواصل", "واتساب", "whatsapp"]
            normalized_reply = normalize_arabic(reply_text)
            if any(kw in normalized_reply for kw in sale_keywords):
                log = ActivityEvent(account_id=account.id, event_type="POLICY_BLOCKED_SALES", details=f"Blocked sales reply to new user: {reply_text[:20]}...")
                session.add(log)
                user.last_reply_status = "SAFETY_BLOCKED_SALES"
                session.add(user)
                await session.commit()
                return

        # --- 6) Repetition Detection ---
        if not await check_reply_repetition(account.id, reply_text):
            await set_account_safe_mode(account.id, True)
            log = ActivityEvent(account_id=account.id, event_type="SAFE_MODE_TRIGGERED", details="Repetition detected")
            session.add(log)
            user.last_reply_status = "ACCOUNT_QUARANTINED"
            session.add(user)
            await session.commit()
            return

        # --- 6) Conversation Diversity Guard (Check) ---
        # If pattern is repeating, maybe escalate
        # We tracked reply above.
        # Here we just log if diversity is low.
        struct_hash = hash(f"{text[:10]}-{reply_text[:10]}")
        if await check_conversation_diversity(account.id, str(struct_hash)):
            # Escalation
            if random.random() < 0.3:
                 await set_human_takeover(sender_id, True)
                 return

        lock_key = f"outgoing:{sender_id}:{hash(reply_text)}"
        if await acquire_queue_lock(lock_key):
            try:
                # --- 4) Random Response Timing ---
                if msg_count <= 1 and random.random() < 0.1:
                    log = ActivityEvent(account_id=account.id, event_type="RANDOM_IGNORE", details="Simulated human miss")
                    session.add(log)
                    user.last_reply_status = "RANDOM_IGNORE"
                    session.add(user)
                    await session.commit()
                    
                    # --- 5) Customer Transparency Layer ---
                    # 10% chance to send "Welcome" instead of full ignore (if it was first msg)
                    if random.random() < 0.1:
                        owner_texts = await get_owner_texts(session, account.id)
                        await enqueue_message(sender_id, owner_texts["soft_welcome_text"], account.id, delay=2)
                        # No further auto-reply
                    return

                # Adaptive Delay
                delay = random.uniform(4, 25) + adaptive_delay
                
                # Check Global App Risk (Reputation Protection)
                if await is_app_risk_high():
                    delay = random.uniform(30, 90) # Severe slowdown
                    # 50% chance to drop auto-reply to protect app
                    if random.random() < 0.5:
                         logger.warning("Global App Risk High: Dropping auto-reply.")
                         # Log
                         log = ActivityEvent(account_id=account.id, event_type="APP_REPUTATION_PROTECTION", details="Dropped reply due to High App Risk")
                         session.add(log)
                         await session.commit()
                         return

                if is_global_risk:
                    delay = random.uniform(15, 60) # Slow Mode
                    logger.warning("Global Protection Active: Increased delay.")
                    
                # Check Neglect Throttle (Merchant Responsibility)
                from app.core.redis_utils import get_redis_client
                redis = await get_redis_client()
                if await redis.exists(f"throttle_neglect:{account.id}"):
                    delay += 20 # Add 20s penalty
                    
                # Traffic Spike Containment (Check rate for account)
                # If > 100 in last minute? (Simulated check)
                q_len = await redis.llen(f"queue:{account.id}")
                if q_len > 50: # Spike
                    delay += 10
                    # Log once per minute?
                    # Simplified: just delay.
                
                await enqueue_message(sender_id, reply_text, account.id, delay=delay)
                
                # Record Global Stats (Success)
                await record_global_behavior(response_time=delay, is_human=False, is_ignored=False)
                
                # --- Record Global Reply Pattern (App Reputation) ---
                await record_global_reply_pattern(account.id, reply_text)
                
                # Track Depth (Outgoing)
                await track_conversation_depth(account.id, conv.id, "outgoing")
                
                reply_msg = Message(
                    account_id=account.id,
                    user_id=user.id,
                    content=reply_text,
                    direction=MessageDirection.OUTGOING
                )
                session.add(reply_msg)
                
                user.last_reply_status = "REPLIED"
                session.add(user)
                await session.commit()
            except Exception as e:
                logger.error(f"Failed to enqueue reply: {e}")
        else:
            logger.warning(f"Duplicate reply prevented to {sender_id}")
    else:
        user.last_reply_status = "INTENT_NOT_DETECTED"
        session.add(user)
        await session.commit()
        await notify_live_chat(account.id, sender_id, text, user.full_name or "User")
        
        # Record Global Stats (Ignored/Human)
        await record_global_behavior(response_time=0, is_human=True, is_ignored=True)
        
        # --- 2) Smart Fallback Response ---
        # 30% chance to send polite fallback
        if random.random() < 0.3:
            owner_texts = await get_owner_texts(session, account.id)
            fallback_text = owner_texts["fallback_text"]
            # Check repetition of fallback to same user?
            # Or assume 30% is low enough.
            # Avoid sending if user sent many messages recently?
            # Let's just send.
            await enqueue_message(sender_id, fallback_text, account.id, delay=5)
            
        # --- 3) Dynamic Intent Learning ---
        # Record unknown message frequency
        from app.core.redis_utils import record_unknown_intent
        is_suggested = await record_unknown_intent(account.id, text)
        if is_suggested:
            # Notify Admin
             log = ActivityEvent(account_id=account.id, event_type="INTENT_SUGGESTION", details=f"Frequent unknown: {text}")
             session.add(log)
             await session.commit()

async def get_or_create_user(
    session: AsyncSession,
    ig_id: str,
    account_id: int,
    send_welcome: bool = True,
) -> User:
    stmt = select(User).where(User.ig_id == ig_id, User.account_id == account_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    
    if not user:
        user = User(ig_id=ig_id, account_id=account_id)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        
        log = ActivityEvent(account_id=account_id, event_type="NEW_USER", details=f"IG: {ig_id}")
        session.add(log)
        await session.commit()
        if send_welcome:
            owner_texts = await get_owner_texts(session, account_id)
            await enqueue_message(ig_id, owner_texts["welcome_text"], account_id)
        
    return user



from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, delete, func, update, desc
from app.core.database import AsyncSessionLocal
from app.models.all_models import AutoReply, MatchType, User, Message as DBMessage, Setting, AdminRole, AdminUser, MessageDirection, Account, DailyStat, ActivityEvent
from app.bot.keyboards import main_menu_keyboard, match_type_keyboard, cancel_keyboard
from app.core.config import settings
from app.core.security import get_admin_role, log_admin_action
from app.services.account_settings import (
    delete_comment_dm_rule,
    get_comment_dm_rules,
    get_owner_texts,
    reset_owner_text,
    set_owner_text,
    upsert_comment_dm_rule,
)
from app.core.redis_utils import (
    enqueue_message, 
    set_human_takeover, 
    get_metrics, 
    set_account_safe_mode, 
    set_account_quarantine,
    get_redis_client,
    check_circuit_breaker,
    record_admin_reply,
    set_global_kill_switch,
    is_global_kill_switch_active,
    set_account_lockdown,
    is_account_locked
)
from datetime import date
import sys
import logging
import json
import time

router = Router()
logger = logging.getLogger(__name__)

class AddReplyState(StatesGroup):
    choosing_type = State()
    entering_keyword = State()
    entering_response = State()

class PauseUserState(StatesGroup):
    entering_id = State()

class LiveChatState(StatesGroup):
    replying_to_user = State()

class CommentRuleState(StatesGroup):
    entering_keyword = State()
    entering_response = State()
    deleting_keyword = State()

class OwnerTextState(StatesGroup):
    entering_value = State()

OWNER_TEXT_LABELS = {
    "welcome_text": "رسالة الترحيب لأول مستخدم جديد",
    "fallback_text": "رسالة عدم فهم النية (Fallback)",
    "soft_welcome_text": "رسالة التحية الخفيفة",
}

def comment_dm_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ إضافة كلمة تعليق", callback_data="comment_dm_add")],
        [InlineKeyboardButton(text="🗑 حذف كلمة تعليق", callback_data="comment_dm_delete")],
        [InlineKeyboardButton(text="🔄 تحديث", callback_data="comment_dm_menu")],
        [InlineKeyboardButton(text="⬅️ رجوع", callback_data="main_menu")],
    ])

def owner_texts_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ تعديل رسالة الترحيب", callback_data="owner_text_edit:welcome_text")],
        [InlineKeyboardButton(text="✏️ تعديل رسالة Fallback", callback_data="owner_text_edit:fallback_text")],
        [InlineKeyboardButton(text="✏️ تعديل التحية الخفيفة", callback_data="owner_text_edit:soft_welcome_text")],
        [InlineKeyboardButton(text="🔄 تحديث", callback_data="owner_texts_menu")],
        [InlineKeyboardButton(text="⬅️ رجوع", callback_data="main_menu")],
    ])

def _truncate_text(text: str, max_len: int = 70) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."

def _build_comment_rules_summary(rules: list[dict[str, str]]) -> str:
    if not rules:
        return "لا توجد قواعد حالياً."
    rows = []
    for idx, rule in enumerate(rules[:20], start=1):
        rows.append(f"{idx}. {rule['keyword']} -> {_truncate_text(rule['response'], 50)}")
    return "\n".join(rows)

def _build_owner_texts_summary(owner_texts: dict[str, str]) -> str:
    rows = []
    for key, label in OWNER_TEXT_LABELS.items():
        rows.append(f"• {label}: {_truncate_text(owner_texts[key], 70)}")
    return "\n".join(rows)

async def check_permission(message: Message, required_roles: list[AdminRole]) -> bool:
    telegram_id = message.from_user.id
    async with AsyncSessionLocal() as session:
        role = await get_admin_role(session, telegram_id)
        if role in required_roles or role == AdminRole.OWNER:
            return True
    return False

# --- Helper to get Account ID for Admin ---
async def get_admin_account_id(session, telegram_id) -> int | None:
    stmt = select(AdminUser).where(AdminUser.telegram_id == telegram_id)
    result = await session.execute(stmt)
    admin = result.scalar_one_or_none()
    if admin:
        return admin.account_id
    if telegram_id in settings.ADMIN_IDS:
        stmt = select(Account.id).limit(1)
        return await session.scalar(stmt)
    return None

@router.message(CommandStart())
async def cmd_start(message: Message):
    async with AsyncSessionLocal() as session:
        role = await get_admin_role(session, message.from_user.id)
        if not role:
            await message.answer("⛔ ليس لديك صلاحية الوصول لهذا البوت.")
            return
            
    welcome_msg = """
👋 <b>أهلاً بك!</b>

تم ربط حسابك بنجاح.

<b>كيف يعمل البوت؟</b>
البوت سيقوم بالرد فقط على الرسائل التي تحتوي أسئلة حقيقية مثل:
(السعر - الطلب - التفاصيل - التوفر)

لن يرد على الرسائل العامة مثل: "مرحبا" أو "👋" وذلك لحماية حسابك من التقييد.

يمكنك الآن إضافة الردود من القائمة أدناه.
"""
    await message.answer(welcome_msg, reply_markup=main_menu_keyboard(), parse_mode="HTML")

# --- Operations Control Panel (Super Admin) ---

@router.message(Command("health_report"))
async def health_report(message: Message):
    if not await check_permission(message, [AdminRole.OWNER]):
        return
        
    redis = await get_redis_client()
    safe_mode = await redis.exists("global_safe_mode")
    
    # Get active accounts
    active_set = await redis.smembers("active_accounts_set")
    
    text = f"🏥 <b>Health Report</b>\n\n"
    text += f"Status: {'🔴 PAUSED' if safe_mode else '🟢 RUNNING'}\n"
    text += f"Active Queues: {len(active_set)}\n\n"
    
    # Check loaded accounts
    high_load = []
    for acc_id in active_set:
        q_len = await redis.llen(f"queue:{acc_id}")
        if q_len > 100:
            high_load.append(f"Acc {acc_id}: {q_len}")
            
    if high_load:
        text += "⚠️ <b>High Load Accounts:</b>\n" + "\n".join(high_load)
    else:
        text += "✅ System Load Normal"
        
    await message.answer(text, parse_mode="HTML")

@router.message(Command("account_status"))
async def account_status(message: Message):
    if not await check_permission(message, [AdminRole.OWNER]):
        return
        
    try:
        acc_id = int(message.text.split()[1])
    except:
        await message.answer("Usage: /account_status {id}")
        return
        
    metrics = await get_metrics(acc_id)
    
    text = f"📊 <b>Account {acc_id} Status</b>\n\n"
    text += f"Circuit: {metrics['circuit_state']}\n"
    text += f"Queue Size: {metrics['queue_size']}\n"
    text += f"Avg Proc Time: {metrics['processing_time_ms']} ms\n"
    text += f"Retries: {metrics['retry_count']}\n"
    text += f"Failed: {metrics['failed_requests']}"
    
    await message.answer(text, parse_mode="HTML")

@router.message(Command("throttle_account"))
async def throttle_account(message: Message):
    if not await check_permission(message, [AdminRole.OWNER]):
        return
    # Not fully implemented manual throttle override, but we can set safe mode
    await message.answer("Use /quarantine {id} to stop processing.")

@router.message(Command("quarantine"))
async def quarantine_cmd(message: Message):
    if not await check_permission(message, [AdminRole.OWNER]):
        return
    try:
        acc_id = int(message.text.split()[1])
        await set_account_quarantine(acc_id, True)
        await message.answer(f"🚫 Account {acc_id} Quarantined.")
    except:
        await message.answer("Usage: /quarantine {id}")

@router.message(Command("unquarantine"))
async def unquarantine_cmd(message: Message):
    if not await check_permission(message, [AdminRole.OWNER]):
        return
    try:
        acc_id = int(message.text.split()[1])
        await set_account_quarantine(acc_id, False)
        await message.answer(f"✅ Account {acc_id} Released.")
    except:
        await message.answer("Usage: /unquarantine {id}")

@router.message(Command("deadletters"))
async def show_deadletters(message: Message):
    if not await check_permission(message, [AdminRole.OWNER]):
        return
    try:
        acc_id = int(message.text.split()[1])
        redis = await get_redis_client()
        key = f"dead_letter:{acc_id}"
        items = await redis.lrange(key, -5, -1) # Last 5
        
        if not items:
            await message.answer("No dead letters.")
            return
            
        text = f"💀 <b>Dead Letters (Acc {acc_id})</b>\n\n"
        for item in items:
            data = json.loads(item)
            text += f"To: {data.get('recipient_id')}\nErr: {data.get('error')}\n\n"
            
        await message.answer(text, parse_mode="HTML")
    except:
        await message.answer("Usage: /deadletters {id}")

@router.message(Command("safety_status"))
async def safety_status(message: Message):
    if not await check_permission(message, [AdminRole.OWNER, AdminRole.MANAGER]):
        return
        
    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, message.from_user.id)
        if not acc_id:
            await message.answer("⚠️ No account linked.")
            return

        redis = await get_redis_client()
        today = date.today().strftime("%Y-%m-%d")
        limit_key = f"daily_conv_limit:{acc_id}:{today}"
        
        current_conv = await redis.get(limit_key) or 0
        from app.core.redis_utils import is_account_quarantined
        is_q = await is_account_quarantined(acc_id)
        
        text = f"🛡️ <b>Safety Status (Acc {acc_id})</b>\n\n"
        text += f"Daily Conversations: {current_conv}/120\n"
        text += f"Status: {'🛑 QUARANTINED' if is_q else '✅ SECURE'}\n"
        
        await message.answer(text, parse_mode="HTML")

@router.message(Command("safety_logs"))
async def safety_logs(message: Message):
    if not await check_permission(message, [AdminRole.OWNER, AdminRole.MANAGER]):
        return

    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, message.from_user.id)
        if not acc_id:
            await message.answer("⚠️ No account linked.")
            return
            
        stmt = select(ActivityEvent).where(
            ActivityEvent.account_id == acc_id,
            ActivityEvent.event_type.in_([
                "POLICY_BLOCKED_SALES", 
                "SAFE_MODE_TRIGGERED", 
                "HUMAN_ESCALATION", 
                "DAILY_LIMIT_REACHED"
            ])
        ).order_by(desc(ActivityEvent.created_at)).limit(10)
        
        result = await session.execute(stmt)
        events = result.scalars().all()
        
        if not events:
            await message.answer("✅ No safety violations detected.")
            return
            
        text = "🚨 <b>Recent Safety Events</b>\n\n"
        for e in events:
            text += f"⚠️ {e.event_type}\n{e.details}\n🕒 {e.created_at.strftime('%H:%M')}\n\n"
            
        await message.answer(text, parse_mode="HTML")

@router.message(Command("force_human"))
async def force_human_cmd(message: Message):
    if not await check_permission(message, [AdminRole.OWNER, AdminRole.MANAGER]):
        return
        
    try:
        parts = message.text.split()
        if len(parts) < 2:
             await message.answer("Usage: /force_human {ig_id}")
             return
             
        ig_id = parts[1]
        await set_human_takeover(ig_id, True)
        
        async with AsyncSessionLocal() as session:
            acc_id = await get_admin_account_id(session, message.from_user.id)
            stmt = select(User).where(User.ig_id == ig_id, User.account_id == acc_id)
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()
            if user:
                user.is_paused = True
                await session.commit()
                
        await message.answer(f"👤 User {ig_id} forced to Human Mode.")
    except Exception as e:
        logger.error(f"Force human error: {e}")
        await message.answer("Error executing command.")

@router.message(Command("why"))
async def why_cmd(message: Message):
    if not await check_permission(message, [AdminRole.OWNER, AdminRole.MANAGER]):
        return
        
    try:
        parts = message.text.split()
        if len(parts) < 2:
             await message.answer("Usage: /why {ig_id}")
             return
             
        ig_id = parts[1]
        
        async with AsyncSessionLocal() as session:
            acc_id = await get_admin_account_id(session, message.from_user.id)
            
            # Fetch last 5 messages/events
            # We can check ActivityEvents for this user or Messages table
            # ActivityEvent is better for "decisions"
            
            stmt = select(ActivityEvent).where(
                ActivityEvent.account_id == acc_id,
                ActivityEvent.details.contains(ig_id) # Simple filter, ideally use user_id link
            ).order_by(desc(ActivityEvent.created_at)).limit(5)
            
            result = await session.execute(stmt)
            events = result.scalars().all()
            
            if not events:
                await message.answer("ℹ️ No recent decision logs found for this user.")
                return
                
            text = f"🧐 <b>Decision Log for {ig_id}</b>\n\n"
            for e in events:
                explanation = "Unknown"
                if "AUTO_REPLY" in e.event_type: explanation = "✅ Replied (Matched Rule)"
                elif "HUMAN" in e.event_type: explanation = "👤 Sent to Human (Escalation/Mode)"
                elif "BLOCKED" in e.event_type: explanation = "🛡️ Blocked (Safety/Policy)"
                elif "LIMIT" in e.event_type: explanation = "⚠️ Limit Reached"
                elif "INTENT" in e.event_type: explanation = "❓ Intent Not Understood"
                elif "IGNORE" in e.event_type: explanation = "🎲 Ignored (Random/Human Like)"
                
                text += f"⏰ {e.created_at.strftime('%H:%M')}\nType: <code>{e.event_type}</code>\nWhy: {explanation}\n\n"
                
            await message.answer(text, parse_mode="HTML")
            
    except Exception as e:
        logger.error(f"Why cmd error: {e}")
        await message.answer("Error executing command.")

@router.message(Command("timeline"))
async def timeline_cmd(message: Message):
    if not await check_permission(message, [AdminRole.OWNER, AdminRole.MANAGER]):
        return
        
    try:
        parts = message.text.split()
        target_acc_id = None
        
        # If admin provides account ID (for Super Admin debugging)
        if len(parts) > 1 and parts[1].isdigit():
            target_acc_id = int(parts[1])
        else:
            # Use their own account
            async with AsyncSessionLocal() as session:
                target_acc_id = await get_admin_account_id(session, message.from_user.id)
                
        if not target_acc_id:
             await message.answer("⚠️ Account not found.")
             return

        async with AsyncSessionLocal() as session:
            # Fetch last 20 events
            stmt = select(ActivityEvent).where(
                ActivityEvent.account_id == target_acc_id
            ).order_by(desc(ActivityEvent.created_at)).limit(20)
            
            result = await session.execute(stmt)
            events = result.scalars().all()
            
            if not events:
                await message.answer("📭 Timeline is empty.")
                return
                
            text = f"🕰️ <b>Timeline Replay (Acc {target_acc_id})</b>\n\n"
            
            # Show oldest first? Or newest first?
            # Debugging usually needs newest first to see "what just happened".
            for e in events:
                time_str = e.created_at.strftime("%H:%M:%S")
                icon = "🔹"
                if "BLOCKED" in e.event_type: icon = "🛡️"
                elif "HUMAN" in e.event_type: icon = "👤"
                elif "FAILED" in e.event_type: icon = "❌"
                elif "AUTO" in e.event_type: icon = "🤖"
                elif "NEGLECT" in e.event_type: icon = "🛑"
                elif "SPIKE" in e.event_type: icon = "📈"
                elif "REPUTATION" in e.event_type: icon = "🚨"
                
                text += f"{icon} <code>{time_str}</code> <b>{e.event_type}</b>\n└ {e.details}\n"
                
            await message.answer(text, parse_mode="HTML")
            
    except Exception as e:
        logger.error(f"Timeline error: {e}")
        await message.answer("Error executing timeline.")

@router.message(Command("setup_check"))
async def setup_check_cmd(message: Message):
    if not await check_permission(message, [AdminRole.OWNER]):
        return
        
    await message.answer("🔄 Checking Setup Readiness...")
    
    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, message.from_user.id)
        stmt = select(Account).where(Account.id == acc_id)
        res = await session.execute(stmt)
        account = res.scalar_one_or_none()
        
        if not account:
            await message.answer("❌ No account found.")
            return
            
        checks = []
        
        # 1. Token Presence
        if account.access_token:
            checks.append("✅ Access Token Present")
        else:
            checks.append("❌ Access Token Missing")
            
        # 2. Page ID
        if account.instagram_page_id:
            checks.append("✅ Page ID Linked")
        else:
            checks.append("❌ Page ID Missing")
            
        # 3. Webhook (Simulated check, check last received)
        redis = await get_redis_client()
        last_ts = await redis.get("last_webhook_ts")
        if last_ts:
            checks.append(f"✅ Webhook Active (Last: {int(time.time() - float(last_ts))}s ago)")
        else:
            checks.append("⚠️ Webhook Not Detected Yet")
            
        # 4. Plan
        if account.plan_id:
            checks.append("✅ Subscription Plan Active")
        else:
            checks.append("⚠️ No Plan Assigned (Using Default)")
            
        await message.answer("📋 <b>Setup Report:</b>\n\n" + "\n".join(checks), parse_mode="HTML")

@router.message(Command("suggested_intents"))
async def suggested_intents_cmd(message: Message):
    if not await check_permission(message, [AdminRole.OWNER, AdminRole.MANAGER]):
        return
        
    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, message.from_user.id)
        if not acc_id:
             await message.answer("⚠️ No account linked.")
             return
             
        redis = await get_redis_client()
        # Scan for suggested intents
        # Keys: unknown_text:{acc_id}:{hash}
        
        cursor = 0
        pattern = f"unknown_text:{acc_id}:*"
        found_texts = []
        
        # Redis SCAN returns a list of keys in the current batch
        while True:
            cursor, keys = await redis.scan(cursor, match=pattern, count=100)
            for key in keys:
                text = await redis.get(key)
                text_hash = key.split(":")[-1]
                count = await redis.get(f"unknown_intent:{acc_id}:{text_hash}")
                if count and int(count) >= 5:
                    found_texts.append(f"🔸 ({count} times): {text}")
            if cursor == 0:
                break
                
        if not found_texts:
            await message.answer("✅ No suggested intents found.")
        else:
            await message.answer("💡 <b>Suggested Intents (Frequent Unknowns)</b>:\n\n" + "\n".join(found_texts), parse_mode="HTML")

@router.message(Command("human_status"))
async def human_status_cmd(message: Message):
     if not await check_permission(message, [AdminRole.OWNER, AdminRole.MANAGER]):
        return
     
     parts = message.text.split()
     if len(parts) < 2:
         await message.answer("Usage: /human_status {ig_id}")
         return
         
     ig_id = parts[1]
     redis = await get_redis_client()
     
     is_active = await redis.exists(f"human_mode:{ig_id}")
     last_reply = await redis.get(f"last_admin_reply:{ig_id}")
     
     status = "🟢 Active (Human Mode)" if is_active else "⚪ Inactive (Auto Mode)"
     last_reply_str = "Never"
     if last_reply:
         elapsed = int(time.time()) - int(last_reply)
         last_reply_str = f"{elapsed}s ago"
         
     await message.answer(f"👤 <b>Human Status for {ig_id}</b>\n\nState: {status}\nLast Admin Reply: {last_reply_str}\nAuto-Recovery: {'Pending (>15m)' if is_active else 'N/A'}", parse_mode="HTML")

@router.message(Command("restore_bot"))
async def restore_bot_cmd(message: Message):
     if not await check_permission(message, [AdminRole.OWNER, AdminRole.MANAGER]):
        return
     
     parts = message.text.split()
     if len(parts) < 2:
         await message.answer("Usage: /restore_bot {ig_id}")
         return
         
     ig_id = parts[1]
     await set_human_takeover(ig_id, False)
     
     async with AsyncSessionLocal() as session:
         acc_id = await get_admin_account_id(session, message.from_user.id)
         stmt = select(User).where(User.ig_id == ig_id, User.account_id == acc_id)
         res = await session.execute(stmt)
         user = res.scalar_one_or_none()
         if user:
             user.is_paused = False
             await session.commit()
             
     await message.answer(f"🤖 Bot restored for {ig_id}.")

# --- Existing Handlers ---

# --- Stats (Enhanced) ---
@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, callback.from_user.id)
        if not acc_id:
            await callback.answer("⚠️ لم يتم العثور على حساب مرتبط.")
            return

        today = date.today()
        stmt = select(DailyStat).where(DailyStat.account_id == acc_id, DailyStat.date == today)
        result = await session.execute(stmt)
        daily = result.scalar_one_or_none()
        
        user_count = await session.scalar(select(func.count(User.id)).where(User.account_id == acc_id))
        msg_count = await session.scalar(select(func.count(DBMessage.id)).where(DBMessage.account_id == acc_id))
        reply_count = await session.scalar(select(func.count(AutoReply.id)).where(AutoReply.account_id == acc_id))
    
    stats_text = f"📊 <b>إحصائيات اليوم ({today})</b>:\n"
    if daily:
        stats_text += f"➕ مستخدمين جدد: {daily.new_users}\n"
        stats_text += f"🤖 ردود تلقائية: {daily.auto_replies}\n"
        stats_text += f"👤 ردود بشرية: {daily.human_replies}\n"
        stats_text += f"🚫 رسائل متجاهلة: {daily.ignored_messages}\n"
    else:
        stats_text += "لا توجد بيانات لهذا اليوم بعد.\n"
        
    stats_text += f"\n📦 <b>الإجمالي (Account {acc_id})</b>:\n"
    stats_text += f"👥 المستخدمين: {user_count}\n"
    stats_text += f"💬 الرسائل: {msg_count}\n"
    stats_text += f"⚙️ قواعد الرد: {reply_count}"

    await callback.message.answer(stats_text, reply_markup=main_menu_keyboard(), parse_mode="HTML")

# --- Activity Feed ---
@router.message(Command("activity"))
async def show_activity(message: Message):
    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, message.from_user.id)
        if not acc_id:
            await message.answer("⚠️ لم يتم العثور على حساب مرتبط.")
            return
            
        stmt = select(ActivityEvent).where(ActivityEvent.account_id == acc_id).order_by(desc(ActivityEvent.created_at)).limit(20)
        result = await session.execute(stmt)
        events = result.scalars().all()
        
        if not events:
            await message.answer("📭 لا توجد نشاطات مسجلة.")
            return
            
        text = f"📋 <b>آخر 20 نشاط (Account {acc_id})</b>:\n\n"
        for event in events:
            time_str = event.created_at.strftime("%H:%M:%S")
            icon = "ℹ️"
            if "AUTO_REPLY" in event.event_type: icon = "🤖"
            elif "HUMAN" in event.event_type: icon = "👤"
            elif "IGNORED" in event.event_type: icon = "🚫"
            elif "LIMIT" in event.event_type: icon = "⚠️"
            elif "NEW_USER" in event.event_type: icon = "➕"
            
            text += f"{icon} <code>{time_str}</code> | <b>{event.event_type}</b>\n{event.details or ''}\n\n"
            
        await message.answer(text, parse_mode="HTML")

# --- Support & Legal Handlers ---

@router.callback_query(F.data == "terms")
async def show_terms(callback: CallbackQuery):
    msg = """
<b>📄 شروط الاستخدام (Terms of Service)</b>

آخر تحديث: 2026

باستخدامك هذه الخدمة فأنت توافق على الشروط التالية:

<b>طبيعة الخدمة</b>
الخدمة هي أداة مساعدة لإدارة رسائل إنستغرام بشكل آلي وشبه آلي. الخدمة لا تمثل إنستغرام ولا تتبع لشركة Meta.

<b>المسؤولية عن الحساب</b>
أنت المسؤول الكامل عن محتوى الردود وطريقة الاستخدام. الخدمة لا تتحمل مسؤولية حظر الحساب أو تقليل الوصول.

<b>حدود الأتمتة</b>
النظام مصمم كخدمة عملاء وليس أداة سبام. الاستخدام المخالف يؤدي لإيقاف الخدمة.

<b>التوفر</b>
لا نضمن توفر 100% وقد تحدث توقفات للصيانة.

<b>البيانات</b>
نستخدم البيانات لتشغيل النظام فقط ولا نبيعها لطرف ثالث.

🔗 <a href="{base_url}/terms">عرض النص الكامل</a>
""".format(base_url=settings.BASE_URL if hasattr(settings, 'BASE_URL') else "https://yazma.com") # Fallback URL
    
    await callback.message.answer(msg, parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer()

@router.callback_query(F.data == "privacy")
async def show_privacy(callback: CallbackQuery):
    msg = """
<b>🔒 سياسة الخصوصية (Privacy Policy)</b>

آخر تحديث: 2026

نحترم خصوصيتك ونلتزم بحماية بياناتك.

<b>البيانات المجمعة:</b>
معرف الحساب، الرسائل الواردة/الصادرة، إعدادات الردود.

<b>ما لا نجمعه:</b>
كلمات المرور، البريد الشخصي، أرقام هواتف العملاء.

<b>استخدام البيانات:</b>
لتشغيل الردود وتحسين الأداء وعرض الإحصائيات فقط. لا نشاركها مع أطراف ثالثة.

<b>الأمان:</b>
يتم تشفير التوكنات وتخزينها بأمان.

🔗 <a href="{base_url}/privacy">عرض النص الكامل</a>
""".format(base_url=settings.BASE_URL if hasattr(settings, 'BASE_URL') else "https://yazma.com")
    
    await callback.message.answer(msg, parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer()

@router.callback_query(F.data == "support")
async def show_support(callback: CallbackQuery):
    msg = """
<b>📞 الدعم الفني</b>

للتواصل مع الدعم الفني:

📧 <code>imedkablavi.info</code>

يرجى إرسال:
1. اسم الحساب (أو ID)
2. وصف المشكلة

سيتم الرد بأقرب وقت ممكن.
"""
    await callback.message.answer(msg, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "why_no_reply_help")
async def why_no_reply_help(callback: CallbackQuery):
    msg = """
<b>❓ لماذا لم يرد البوت؟</b>

البوت لم يرد لأحد الأسباب التالية:

• <b>الرسالة ليست سؤالاً واضحاً:</b> البوت يتجاهل "مرحبا" والرموز التعبيرية.
• <b>خارج نافذة 24 ساعة:</b> سياسات إنستغرام تمنع الرد الآلي بعد مرور 24 ساعة على آخر رسالة من العميل.
• <b>الرد البشري:</b> تم تحويل المحادثة لك للرد البشري (Human Mode).
• <b>الحماية:</b> تم إيقاف الرد لحماية الحساب مؤقتاً (Safe Mode).

النظام يعمل كخدمة عملاء وليس روبوت سبام.
"""
    await callback.message.answer(msg, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "main_menu")
async def back_to_main_menu(callback: CallbackQuery):
    await callback.message.edit_text("القائمة الرئيسية:", reply_markup=main_menu_keyboard())
    await callback.answer()

# --- Comment DM Rules ---
@router.callback_query(F.data == "comment_dm_menu")
async def comment_dm_menu(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        role = await get_admin_role(session, callback.from_user.id)
        if role not in [AdminRole.OWNER, AdminRole.MANAGER]:
            await callback.answer("⛔ ليس لديك صلاحية.")
            return

        acc_id = await get_admin_account_id(session, callback.from_user.id)
        if not acc_id:
            await callback.answer("⚠️ لا يوجد حساب مرتبط.")
            return

        rules = await get_comment_dm_rules(session, acc_id)

    text = "💬 <b>قواعد الرد الخاص على التعليقات</b>\n\n"
    text += (
        "عند كتابة المستخدم تعليق يحتوي كلمة مفتاحية، "
        "سيتم إرسال رسالة خاصة له تلقائياً.\n\n"
    )
    text += _build_comment_rules_summary(rules)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=comment_dm_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data == "comment_dm_add")
async def comment_dm_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CommentRuleState.entering_keyword)
    await callback.message.answer(
        "✏️ أرسل الكلمة المفتاحية التي يجب التقاطها من التعليق.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()

@router.message(CommentRuleState.entering_keyword)
async def comment_dm_receive_keyword(message: Message, state: FSMContext):
    keyword = (message.text or "").strip()
    if not keyword:
        await message.answer("⚠️ الكلمة المفتاحية لا يمكن أن تكون فارغة.")
        return

    await state.update_data(comment_keyword=keyword)
    await state.set_state(CommentRuleState.entering_response)
    await message.answer(
        "✏️ أرسل نص الرسالة الخاصة التي ستُرسل عند مطابقة هذا التعليق.",
        reply_markup=cancel_keyboard(),
    )

@router.message(CommentRuleState.entering_response)
async def comment_dm_receive_response(message: Message, state: FSMContext):
    response = (message.text or "").strip()
    if not response:
        await message.answer("⚠️ نص الرد لا يمكن أن يكون فارغاً.")
        return

    data = await state.get_data()
    keyword = data.get("comment_keyword")
    if not keyword:
        await state.clear()
        await message.answer("⚠️ انتهت الجلسة. أعد المحاولة.", reply_markup=main_menu_keyboard())
        return

    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, message.from_user.id)
        if not acc_id:
            await state.clear()
            await message.answer("⚠️ لا يوجد حساب مرتبط.", reply_markup=main_menu_keyboard())
            return

        inserted = await upsert_comment_dm_rule(session, acc_id, keyword, response)
        await session.commit()
        await log_admin_action(
            session,
            message.from_user.id,
            "comment_dm_rule_upsert",
            f"keyword={keyword} inserted={inserted}",
        )
        rules = await get_comment_dm_rules(session, acc_id)

    await state.clear()
    status = "✅ تمت إضافة القاعدة." if inserted else "✅ تم تحديث القاعدة."
    text = f"{status}\n\n💬 <b>القواعد الحالية:</b>\n{_build_comment_rules_summary(rules)}"
    await message.answer(text, parse_mode="HTML", reply_markup=comment_dm_menu_keyboard())

@router.callback_query(F.data == "comment_dm_delete")
async def comment_dm_delete_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CommentRuleState.deleting_keyword)
    await callback.message.answer(
        "🗑 أرسل الكلمة المفتاحية التي تريد حذف قاعدتها.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()

@router.message(CommentRuleState.deleting_keyword)
async def comment_dm_delete_execute(message: Message, state: FSMContext):
    keyword = (message.text or "").strip()
    if not keyword:
        await message.answer("⚠️ الكلمة المفتاحية لا يمكن أن تكون فارغة.")
        return

    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, message.from_user.id)
        if not acc_id:
            await state.clear()
            await message.answer("⚠️ لا يوجد حساب مرتبط.", reply_markup=main_menu_keyboard())
            return

        deleted = await delete_comment_dm_rule(session, acc_id, keyword)
        if deleted:
            await session.commit()
            await log_admin_action(
                session,
                message.from_user.id,
                "comment_dm_rule_delete",
                f"keyword={keyword}",
            )
        rules = await get_comment_dm_rules(session, acc_id)

    await state.clear()
    status = "✅ تم حذف القاعدة." if deleted else "ℹ️ لم يتم العثور على هذه الكلمة."
    text = f"{status}\n\n💬 <b>القواعد الحالية:</b>\n{_build_comment_rules_summary(rules)}"
    await message.answer(text, parse_mode="HTML", reply_markup=comment_dm_menu_keyboard())

# --- Owner Texts ---
@router.callback_query(F.data == "owner_texts_menu")
async def owner_texts_menu(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        role = await get_admin_role(session, callback.from_user.id)
        if role not in [AdminRole.OWNER, AdminRole.MANAGER]:
            await callback.answer("⛔ ليس لديك صلاحية.")
            return

        acc_id = await get_admin_account_id(session, callback.from_user.id)
        if not acc_id:
            await callback.answer("⚠️ لا يوجد حساب مرتبط.")
            return

        owner_texts = await get_owner_texts(session, acc_id)

    text = "📝 <b>تخصيص نصوص الحساب</b>\n\n"
    text += _build_owner_texts_summary(owner_texts)
    text += "\n\nلإرجاع أي نص للوضع الافتراضي أرسل: <code>/default</code> أثناء التعديل."
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=owner_texts_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data.startswith("owner_text_edit:"))
async def owner_text_edit_start(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key not in OWNER_TEXT_LABELS:
        await callback.answer("⚠️ إعداد غير صالح.")
        return

    await state.update_data(owner_text_key=key)
    await state.set_state(OwnerTextState.entering_value)
    await callback.message.answer(
        f"✏️ أرسل النص الجديد لـ: {OWNER_TEXT_LABELS[key]}\n"
        "لإرجاع النص الافتراضي أرسل: /default",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()

@router.message(OwnerTextState.entering_value)
async def owner_text_edit_save(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    data = await state.get_data()
    text_key = data.get("owner_text_key")

    if text_key not in OWNER_TEXT_LABELS:
        await state.clear()
        await message.answer("⚠️ انتهت الجلسة. أعد المحاولة.", reply_markup=main_menu_keyboard())
        return

    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, message.from_user.id)
        if not acc_id:
            await state.clear()
            await message.answer("⚠️ لا يوجد حساب مرتبط.", reply_markup=main_menu_keyboard())
            return

        if value.lower() == "/default":
            await reset_owner_text(session, acc_id, text_key)
            action = "owner_text_reset"
            details = f"text_key={text_key}"
            status = "✅ تم إرجاع النص للوضع الافتراضي."
        else:
            if not value:
                await message.answer("⚠️ النص لا يمكن أن يكون فارغاً.")
                return
            await set_owner_text(session, acc_id, text_key, value)
            action = "owner_text_update"
            details = f"text_key={text_key}"
            status = "✅ تم حفظ النص الجديد."

        await session.commit()
        await log_admin_action(session, message.from_user.id, action, details)
        owner_texts = await get_owner_texts(session, acc_id)

    await state.clear()
    text = f"{status}\n\n📝 <b>النصوص الحالية:</b>\n{_build_owner_texts_summary(owner_texts)}"
    await message.answer(text, parse_mode="HTML", reply_markup=owner_texts_menu_keyboard())

# --- Add Reply Flow ---
@router.callback_query(F.data == "add_reply")
async def start_add_reply(callback: CallbackQuery, state: FSMContext):
    async with AsyncSessionLocal() as session:
        role = await get_admin_role(session, callback.from_user.id)
        if role not in [AdminRole.OWNER, AdminRole.MANAGER]:
            await callback.answer("⛔ ليس لديك صلاحية.")
            return

    await state.set_state(AddReplyState.choosing_type)
    await callback.message.edit_text("اختر نوع الرد:", reply_markup=match_type_keyboard())

@router.callback_query(F.data.startswith("type_"))
async def process_type(callback: CallbackQuery, state: FSMContext):
    match_type = callback.data.split("_")[1]
    await state.update_data(match_type=match_type)
    
    if match_type == "fallback":
        await state.update_data(keyword="FALLBACK")
        await state.set_state(AddReplyState.entering_response)
        await callback.message.edit_text("✏️ أدخل نص الرد الافتراضي:", reply_markup=cancel_keyboard())
    else:
        await state.set_state(AddReplyState.entering_keyword)
        text = "أدخل الكلمة المفتاحية:" if match_type == "keyword" else "أدخل الجملة للمطابقة التامة:"
        await callback.message.edit_text(f"✏️ {text}", reply_markup=cancel_keyboard())

@router.message(AddReplyState.entering_keyword)
async def process_keyword(message: Message, state: FSMContext):
    await state.update_data(keyword=message.text)
    await state.set_state(AddReplyState.entering_response)
    await message.answer("✏️ أدخل نص الرد:", reply_markup=cancel_keyboard())

@router.message(AddReplyState.entering_response)
async def process_response(message: Message, state: FSMContext):
    data = await state.get_data()
    m_type_str = data['match_type']
    match_type_enum = MatchType.EXACT
    if m_type_str == "keyword": match_type_enum = MatchType.KEYWORD
    elif m_type_str == "fallback": match_type_enum = MatchType.FALLBACK
    
    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, message.from_user.id)
        if not acc_id:
             await message.answer("⚠️ خطأ: لا يوجد حساب مرتبط.")
             await state.clear()
             return

        new_reply = AutoReply(
            account_id=acc_id,
            match_type=match_type_enum,
            keyword=data['keyword'],
            response=message.text,
            is_active=True
        )
        session.add(new_reply)
        await session.commit()
        await log_admin_action(session, message.from_user.id, "add_reply", f"Added {m_type_str} reply: {data['keyword']}")
    
    await state.clear()
    await message.answer("✅ تم حفظ الرد بنجاح!", reply_markup=main_menu_keyboard())

@router.callback_query(F.data == "cancel")
async def cancel_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ تم الإلغاء", reply_markup=main_menu_keyboard())

# --- List Replies ---
@router.callback_query(F.data == "list_replies")
async def list_replies(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, callback.from_user.id)
        if not acc_id:
             await callback.answer("⚠️ خطأ: لا يوجد حساب مرتبط.")
             return
             
        result = await session.execute(select(AutoReply).where(AutoReply.account_id == acc_id))
        replies = result.scalars().all()
    
    if not replies:
        await callback.message.answer("📭 لا توجد ردود محفوظة.", reply_markup=main_menu_keyboard())
        return

    text = "📋 قائمة الردود:\n\n"
    for r in replies:
        type_icon = "🔹" if r.match_type == MatchType.EXACT else "🔸" if r.match_type == MatchType.KEYWORD else "🔻"
        text += f"{type_icon} ID: {r.id} | Type: {r.match_type}\n"
        text += f"Key: {r.keyword}\n"
        text += f"Reply: {r.response[:50]}...\n\n"
    
    await callback.message.answer(text, reply_markup=main_menu_keyboard())

# --- Toggle System (Global) ---
@router.callback_query(F.data == "toggle_system")
async def toggle_system(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        role = await get_admin_role(session, callback.from_user.id)
        if role != AdminRole.OWNER:
             await callback.answer("⛔ فقط المالك يمكنه تغيير حالة النظام.")
             return

        stmt = select(Setting).where(Setting.key == "system_enabled")
        result = await session.execute(stmt)
        setting = result.scalar_one_or_none()
        
        if not setting:
            setting = Setting(key="system_enabled", value="true")
            session.add(setting)
            new_status = True
        else:
            current_val = setting.value == "true"
            new_status = not current_val
            setting.value = "true" if new_status else "false"
        
        await session.commit()
        await log_admin_action(session, callback.from_user.id, "toggle_system", f"New Status: {new_status}")
        
    status_text = "✅ مفعل" if new_status else "🛑 متوقف"
    await callback.message.answer(f"تم تغيير حالة النظام إلى: {status_text}", reply_markup=main_menu_keyboard())

# --- Human Mode (Pause User) ---
@router.callback_query(F.data == "human_mode")
async def human_mode_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PauseUserState.entering_id)
    await callback.message.edit_text("أدخل معرف مستخدم إنستغرام (IG ID) لإيقاف/تفعيل الرد الآلي له:", reply_markup=cancel_keyboard())

@router.message(PauseUserState.entering_id)
async def process_pause_user(message: Message, state: FSMContext):
    ig_id = message.text.strip()
    
    async with AsyncSessionLocal() as session:
        acc_id = await get_admin_account_id(session, message.from_user.id)
        stmt = select(User).where(User.ig_id == ig_id, User.account_id == acc_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        
        if not user:
            await message.answer("❌ المستخدم غير موجود في قاعدة البيانات.", reply_markup=main_menu_keyboard())
        else:
            user.is_paused = not user.is_paused
            await session.commit()
            await set_human_takeover(ig_id, active=user.is_paused)
            status = "🛑 متوقف (Human Mode)" if user.is_paused else "✅ مفعل (Auto Reply)"
            await message.answer(f"تم تحديث حالة المستخدم {user.full_name or ig_id}:\n{status}", reply_markup=main_menu_keyboard())
            await log_admin_action(session, message.from_user.id, "human_mode", f"User: {ig_id}, Paused: {user.is_paused}")
            
    await state.clear()

# --- Kill Switch ---
@router.message(Command("pause_all"))
async def kill_switch(message: Message):
    if not await check_permission(message, [AdminRole.OWNER]):
        return
        
    await set_global_kill_switch(True)
    async with AsyncSessionLocal() as session:
        await log_admin_action(session, message.from_user.id, "kill_switch", "GLOBAL PAUSE ACTIVATED")
    
    await message.answer("⚠️ <b>SYSTEM HALTED</b> (Global Kill Switch Active)\nWorkers paused. Webhooks still received.", parse_mode="HTML")

@router.message(Command("resume_all"))
async def resume_switch(message: Message):
    if not await check_permission(message, [AdminRole.OWNER]):
        return
        
    await set_global_kill_switch(False)
    async with AsyncSessionLocal() as session:
        await log_admin_action(session, message.from_user.id, "kill_switch", "GLOBAL RESUME")
    
    await message.answer("✅ <b>SYSTEM RESUMED</b>", parse_mode="HTML")

# --- System Pause (Global) ---
@router.message(Command("system_pause"))
async def system_pause(message: Message):
    # Alias for pause_all
    await kill_switch(message)

# --- Account Lockdown (Emergency Isolation) ---
@router.message(Command("lock_account"))
async def lock_account_cmd(message: Message):
    if not await check_permission(message, [AdminRole.OWNER]):
        return
    try:
        acc_id = int(message.text.split()[1])
        await set_account_lockdown(acc_id, True)
        await message.answer(f"🔒 Account {acc_id} LOCKED (Processing Stopped).")
    except:
        await message.answer("Usage: /lock_account {id}")

@router.message(Command("unlock_account"))
async def unlock_account_cmd(message: Message):
    if not await check_permission(message, [AdminRole.OWNER]):
        return
    try:
        acc_id = int(message.text.split()[1])
        await set_account_lockdown(acc_id, False)
        await message.answer(f"🔓 Account {acc_id} UNLOCKED.")
    except:
        await message.answer("Usage: /unlock_account {id}")

# --- Live Chat Bridge ---
@router.callback_query(F.data.startswith("reply_to:"))
async def live_chat_reply_start(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) < 3:
         await callback.answer("بيانات غير صالحة.")
         return
         
    account_id = int(parts[1])
    ig_id = parts[2]
    
    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.ig_id == ig_id, User.account_id == account_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        
        if not user:
             await callback.answer("المستخدم غير موجود.")
             return
             
    await state.update_data(reply_ig_id=ig_id, reply_account_id=account_id)
    await state.set_state(LiveChatState.replying_to_user)
    await callback.message.answer(f"📝 أكتب ردك الآن للمستخدم (IG: {ig_id}):\n(سيتم إرساله مباشرة)", reply_markup=cancel_keyboard())
    await callback.answer()

@router.message(LiveChatState.replying_to_user)
async def live_chat_send(message: Message, state: FSMContext):
    data = await state.get_data()
    ig_id = data.get("reply_ig_id")
    account_id = data.get("reply_account_id")
    text = message.text
    
    if not ig_id or not text or not account_id:
        return

    await set_human_takeover(ig_id, active=True)
    await record_admin_reply(ig_id) # Record time for auto-recovery
    await enqueue_message(ig_id, text, account_id)
    
    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.ig_id == ig_id, User.account_id == account_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        
        if user:
            user.is_paused = True 
            
            reply_msg = DBMessage(
                account_id=account_id,
                user_id=user.id,
                content=text,
                direction=MessageDirection.OUTGOING,
                mid=f"manual_{message.message_id}" 
            )
            session.add(reply_msg)
            
            today = date.today()
            stat_stmt = select(DailyStat).where(DailyStat.account_id == account_id, DailyStat.date == today)
            stat_res = await session.execute(stat_stmt)
            stat = stat_res.scalar_one_or_none()
            if not stat:
                stat = DailyStat(account_id=account_id, date=today)
                session.add(stat)
            stat.human_replies += 1
            
            await session.commit()
            
            await log_admin_action(session, message.from_user.id, "manual_reply", f"To: {ig_id} (Acc: {account_id})")

    await message.answer(f"✅ تم الإرسال للمستخدم {ig_id}", reply_markup=main_menu_keyboard())
    await state.clear()

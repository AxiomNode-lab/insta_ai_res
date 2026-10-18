from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo


def main_menu_keyboard():
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ إضافة رد تلقائي", callback_data="add_reply")],
            [InlineKeyboardButton(text="📋 عرض الردود", callback_data="list_replies")],
            [InlineKeyboardButton(text="📊 الإحصائيات", callback_data="stats")],
            [InlineKeyboardButton(text="🛑 إيقاف/تفعيل النظام", callback_data="toggle_system")],
            [InlineKeyboardButton(text="👤 وضع الرد البشري", callback_data="human_mode")],
            [InlineKeyboardButton(text="💬 رد خاص للتعليقات", callback_data="comment_dm_menu")],
            [InlineKeyboardButton(text="🕘 ساعات العمل", callback_data="business_hours_menu")],
            [InlineKeyboardButton(text="📝 تخصيص نصوص الحساب", callback_data="owner_texts_menu")],
            [
                InlineKeyboardButton(text="📄 الشروط", callback_data="terms"),
                InlineKeyboardButton(text="🔒 الخصوصية", callback_data="privacy"),
            ],
            [
                InlineKeyboardButton(text="📞 الدعم الفني", callback_data="support"),
                InlineKeyboardButton(text="❓ لماذا لم يرد؟", callback_data="why_no_reply_help"),
            ],
        ]
    )
    return keyboard


def match_type_keyboard():
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="تطابق تام (Exact)", callback_data="type_exact")],
            [InlineKeyboardButton(text="كلمة مفتاحية (Keyword)", callback_data="type_keyword")],
            [InlineKeyboardButton(text="رد افتراضي (Fallback)", callback_data="type_fallback")],
        ]
    )
    return keyboard


def cancel_keyboard():
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel")]]
    )
    return keyboard

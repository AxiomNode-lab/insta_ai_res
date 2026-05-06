import re
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.all_models import AutoReply, MatchType, User, Setting

def normalize_arabic(text: str) -> str:
    """
    Normalizes Arabic text by removing diacritics and unifying Alef forms.
    """
    if not text:
        return ""
    
    # Remove diacritics (Tashkeel)
    text = re.sub(r'[\u064B-\u065F\u0640]', '', text)
    
    # Unify Alef
    text = re.sub(r'[إأآ]', 'ا', text)
    
    # Unify Yaa/Alif Maqsura
    text = re.sub(r'ى', 'ي', text)
    
    # Unify Taa Marbuta/Ha
    text = re.sub(r'ة', 'ه', text)
    
    return text.strip().lower()

def light_stem(token: str) -> str:
    """
    Performs light stemming for Arabic suffixes.
    Removes: ه, ها, هم, كم, كن, نا, ات, ون, ين
    """
    suffixes = [
        "ها", "هم", "كم", "كن", "نا", "ات", "ون", "ين", "ه"
    ]
    
    # Iterate through suffixes (longest first naturally by list order if we sorted, but manual is fine)
    # Better sort by length desc to match "هم" before "ه"
    suffixes.sort(key=len, reverse=True)
    
    for suffix in suffixes:
        if token.endswith(suffix) and len(token) > len(suffix) + 2: # Keep at least 2 chars root
            return token[:-len(suffix)]
            
    return token

def tokenize(text: str) -> set[str]:
    """
    Splits text into normalized and stemmed tokens.
    """
    normalized = normalize_arabic(text)
    raw_tokens = re.findall(r'\w+', normalized)
    
    stemmed_tokens = set()
    for t in raw_tokens:
        stemmed_tokens.add(light_stem(t))
        
    return stemmed_tokens

def is_intent_to_ask(text: str) -> bool:
    """
    Checks if the message contains intent keywords.
    """
    keywords = ["سعر", "كم", "أين", "اين", "متى", "كيف", "هل", "يوجد", "متوفر", "موقع", "مقاس", "تفاصيل", "بكم"]
    normalized = normalize_arabic(text)
    for kw in keywords:
        if kw in normalized:
            return True
    return False

async def get_auto_reply(
    session: AsyncSession,
    ig_sender_id: str,
    message_text: str,
    account_id: int,
) -> str | None:
    """
    Determines the reply based on the tri-level logic:
    1. Exact Match
    2. Keyword Match (Stemmed Token Based)
    3. NO Fallback (Strict Production Rule)
    """
    
    # -1. Check Global System Status
    stmt = select(Setting).where(Setting.key == "system_enabled")
    result = await session.execute(stmt)
    setting = result.scalar_one_or_none()
    
    # If setting exists and is explicitly "false", stop.
    if setting and setting.value == "false":
        return None

    # 0. Check if user is paused (Human Mode)
    stmt = select(User).where(User.ig_id == ig_sender_id, User.account_id == account_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    
    if user and user.is_paused:
        return None # No auto-reply in human mode

    if not message_text:
        return None
        
    # --- 1) Intent Detection Guard ---
    if not is_intent_to_ask(message_text):
        return None

    normalized_msg = normalize_arabic(message_text)
    msg_tokens = tokenize(message_text)

    # Fetch all active rules
    stmt = select(AutoReply).where(
        AutoReply.is_active == True,
        AutoReply.account_id == account_id,
    )
    result = await session.execute(stmt)
    rules = result.scalars().all()
    
    # 1. Exact Match (Still uses normalized full text for strictness)
    for rule in rules:
        if rule.match_type == MatchType.EXACT:
            if normalize_arabic(rule.keyword) == normalized_msg:
                return rule.response
                
    # 2. Keyword Match (Stemmed Token Based)
    for rule in rules:
        if rule.match_type == MatchType.KEYWORD:
            # Tokenize rule keyword as well to match stemmed forms
            rule_tokens = tokenize(rule.keyword)
            
            # Check if rule tokens are subset of message tokens
            if rule_tokens and rule_tokens.issubset(msg_tokens):
                return rule.response
            
            # Fallback to simple contains for phrases that might not tokenize well
            # But this ignores stemming. Let's rely on token match mostly.
            # Only use contains if token match fails, using normalized text.
            if normalize_arabic(rule.keyword) in normalized_msg:
                 return rule.response

    # 3. Fallback REMOVED
            
    return None

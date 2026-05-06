from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Enum as SQLEnum, ForeignKey, BigInteger, Date, Index, Float
from sqlalchemy.orm import relationship, Mapped, mapped_column
from datetime import datetime, date
import enum
from app.models.base import Base

class MatchType(str, enum.Enum):
    EXACT = "exact"
    KEYWORD = "keyword"
    FALLBACK = "fallback"

class MessageDirection(str, enum.Enum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"

class AdminRole(str, enum.Enum):
    OWNER = "owner"
    MANAGER = "manager"
    AGENT = "agent"

class AccountStatus(str, enum.Enum):
    ACTIVE = "active"
    RESTRICTED = "restricted"
    QUARANTINED = "quarantined"
    DISABLED = "disabled"

class ConversationQuality(str, enum.Enum):
    LOW_QUALITY = "low_quality"
    MEDIUM_QUALITY = "medium_quality"
    HIGH_QUALITY = "high_quality"
    UNKNOWN = "unknown"

class AccountBehaviorState(str, enum.Enum):
    HEALTHY = "healthy"
    DRY_CONVERSATIONS = "dry_conversations"
    BOT_LIKE_PATTERN = "bot_like_pattern"

class ReputationRiskLevel(str, enum.Enum):
    HEALTHY = "healthy"
    WATCHLIST = "watchlist"
    DANGEROUS = "dangerous"

class AccountReputationHistory(Base):
    __tablename__ = "account_reputation_history"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    reputation_score: Mapped[int] = mapped_column(Integer, default=100)
    risk_level: Mapped[ReputationRiskLevel] = mapped_column(SQLEnum(ReputationRiskLevel), default=ReputationRiskLevel.HEALTHY)
    
    # Metrics Snapshot
    avg_depth: Mapped[float] = mapped_column(Float, default=0.0)
    reply_speed_dist: Mapped[str] = mapped_column(Text, nullable=True) # JSON
    human_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    ignored_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    policy_blocks: Mapped[int] = mapped_column(Integer, default=0)
    safety_triggers: Mapped[int] = mapped_column(Integer, default=0)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    account = relationship("Account", back_populates="reputation_history")

class Account(Base):
    __tablename__ = "accounts"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    instagram_page_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    instagram_user_id: Mapped[str] = mapped_column(String, nullable=True) # The IG Business ID
    access_token: Mapped[str] = mapped_column(Text) # Encrypted with Fernet (HMAC key)
    token_version: Mapped[int] = mapped_column(Integer, default=1) # Increments on refresh
    token_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    owner_admin_id: Mapped[int] = mapped_column(Integer, nullable=True) # Linked to AdminUser ID later if needed
    
    plan_id: Mapped[int] = mapped_column(ForeignKey("subscription_plans.id"), nullable=True)
    status: Mapped[AccountStatus] = mapped_column(SQLEnum(AccountStatus), default=AccountStatus.ACTIVE)
    
    # Retention Settings (in days)
    msg_retention_days: Mapped[int] = mapped_column(Integer, default=30)
    user_retention_days: Mapped[int] = mapped_column(Integer, default=90)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    users = relationship("User", back_populates="account")
    auto_replies = relationship("AutoReply", back_populates="account")
    logs = relationship("Log", back_populates="account")
    daily_stats = relationship("DailyStat", back_populates="account")
    activity_events = relationship("ActivityEvent", back_populates="account")
    plan = relationship("SubscriptionPlan")
    last_processed_events = relationship("LastProcessedEvent", back_populates="account")
    
    # Behavior State
    behavior_state: Mapped[AccountBehaviorState] = mapped_column(SQLEnum(AccountBehaviorState), default=AccountBehaviorState.HEALTHY)
    reputation_risk: Mapped[ReputationRiskLevel] = mapped_column(SQLEnum(ReputationRiskLevel), default=ReputationRiskLevel.HEALTHY)
    
    reputation_history = relationship("AccountReputationHistory", back_populates="account")

class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    max_accounts: Mapped[int] = mapped_column(Integer, default=1)
    max_daily_messages: Mapped[int] = mapped_column(Integer, default=100)
    max_users: Mapped[int] = mapped_column(Integer, default=500)
    price_monthly: Mapped[float] = mapped_column(Integer, default=0)

class ActivityEvent(Base):
    __tablename__ = "activity_events"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    event_type: Mapped[str] = mapped_column(String) # NEW_USER, AUTO_REPLY, HUMAN_REPLY, IGNORED, LIMIT_EXCEEDED, MESSAGE_FAILED
    details: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    account = relationship("Account", back_populates="activity_events")

class LastProcessedEvent(Base):
    __tablename__ = "last_processed_events"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True, unique=True)
    last_timestamp: Mapped[str] = mapped_column(String) # Storing as string or bigint from Meta
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    account = relationship("Account", back_populates="last_processed_events")

class User(Base):
    __tablename__ = "users"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True, nullable=True)
    ig_id: Mapped[str] = mapped_column(String, index=True) 
    full_name: Mapped[str] = mapped_column(String, nullable=True)
    username: Mapped[str] = mapped_column(String, nullable=True)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    last_reply_status: Mapped[str] = mapped_column(String, nullable=True) # OUTSIDE_24H_WINDOW, INTENT_NOT_DETECTED, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    account = relationship("Account", back_populates="users")
    messages = relationship("Message", back_populates="user")
    conversations = relationship("Conversation", back_populates="user")
    
    __table_args__ = (
        Index('idx_users_account_ig', 'account_id', 'ig_id'),
    )

class Message(Base):
    __tablename__ = "messages"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True, nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    content: Mapped[str] = mapped_column(Text)
    direction: Mapped[MessageDirection] = mapped_column(SQLEnum(MessageDirection))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    mid: Mapped[str] = mapped_column(String, index=True, nullable=True) 

    user = relationship("User", back_populates="messages")
    
    __table_args__ = (
        Index('idx_messages_account_created', 'account_id', 'timestamp'), # Using timestamp as created_at
        Index('idx_messages_account_user', 'account_id', 'user_id'),
    )

class AutoReply(Base):
    __tablename__ = "auto_replies"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True, nullable=True)
    keyword: Mapped[str] = mapped_column(String, index=True) 
    response: Mapped[str] = mapped_column(Text)
    match_type: Mapped[MatchType] = mapped_column(SQLEnum(MatchType), default=MatchType.EXACT)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    account = relationship("Account", back_populates="auto_replies")
    
    __table_args__ = (
        Index('idx_autoreplies_account_keyword', 'account_id', 'keyword'),
    )

class Conversation(Base):
    __tablename__ = "conversations"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True, nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    state: Mapped[str] = mapped_column(String, default="active")
    last_interaction: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Conversation Depth & Quality
    msg_count_user: Mapped[int] = mapped_column(Integer, default=0)
    msg_count_bot: Mapped[int] = mapped_column(Integer, default=0)
    quality_score: Mapped[ConversationQuality] = mapped_column(SQLEnum(ConversationQuality), default=ConversationQuality.UNKNOWN)
    
    user = relationship("User", back_populates="conversations")
    
    __table_args__ = (
        Index('idx_conversations_account_last_interaction', 'account_id', 'last_interaction'),
    )

class Setting(Base):
    __tablename__ = "settings"
    
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(String, nullable=True)

class Log(Base):
    __tablename__ = "logs"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True, nullable=True)
    level: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    account = relationship("Account", back_populates="logs")

class AdminUser(Base):
    __tablename__ = "admin_users"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True, nullable=True) 
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True) 
    role: Mapped[AdminRole] = mapped_column(SQLEnum(AdminRole), default=AdminRole.AGENT)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class AdminLog(Base):
    __tablename__ = "admin_logs"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True, nullable=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id"))
    action: Mapped[str] = mapped_column(String)
    details: Mapped[str] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Token(Base):
    __tablename__ = "tokens"
    
    service: Mapped[str] = mapped_column(String, primary_key=True) 
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True, nullable=True)
    access_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class DailyStat(Base):
    __tablename__ = "daily_stats"
    
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    new_users: Mapped[int] = mapped_column(Integer, default=0)
    auto_replies: Mapped[int] = mapped_column(Integer, default=0)
    human_replies: Mapped[int] = mapped_column(Integer, default=0)
    ignored_messages: Mapped[int] = mapped_column(Integer, default=0)
    top_keywords: Mapped[str] = mapped_column(Text, nullable=True) 
    
    account = relationship("Account", back_populates="daily_stats")

"""Initial schema for fresh PostgreSQL deployments.

Existing databases created by the legacy ORM auto-create path must be checked
against the ORM schema and stamped to this revision; see docs/DB_MIGRATIONS.md.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261018_01"
down_revision = None
branch_labels = None
depends_on = None


_ENUMS = {
    "matchtype": ("EXACT", "KEYWORD", "FALLBACK"),
    "messagedirection": ("INCOMING", "OUTGOING"),
    "adminrole": ("OWNER", "MANAGER", "AGENT"),
    "accountstatus": ("ACTIVE", "RESTRICTED", "QUARANTINED", "DISABLED"),
    "conversationquality": ("LOW_QUALITY", "MEDIUM_QUALITY", "HIGH_QUALITY", "UNKNOWN"),
    "accountbehaviorstate": ("HEALTHY", "DRY_CONVERSATIONS", "BOT_LIKE_PATTERN"),
    "reputationrisklevel": ("HEALTHY", "WATCHLIST", "DANGEROUS"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, labels in _ENUMS.items():
        postgresql.ENUM(*labels, name=name).create(bind, checkfirst=True)

    op.create_table(
        "subscription_plans",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("max_accounts", sa.Integer(), nullable=False),
        sa.Column("max_daily_messages", sa.Integer(), nullable=False),
        sa.Column("max_users", sa.Integer(), nullable=False),
        sa.Column("price_monthly", sa.Integer(), nullable=False),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_subscription_plans_id", "subscription_plans", ["id"])

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("instagram_page_id", sa.String(), nullable=False),
        sa.Column("instagram_user_id", sa.String(), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("owner_admin_id", sa.Integer(), nullable=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("subscription_plans.id"), nullable=True),
        sa.Column("status", _enum("accountstatus"), nullable=False),
        sa.Column("msg_retention_days", sa.Integer(), nullable=False),
        sa.Column("user_retention_days", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("behavior_state", _enum("accountbehaviorstate"), nullable=False),
        sa.Column("reputation_risk", _enum("reputationrisklevel"), nullable=False),
    )
    op.create_index("ix_accounts_id", "accounts", ["id"])
    op.create_index("ix_accounts_instagram_page_id", "accounts", ["instagram_page_id"], unique=True)

    op.create_table(
        "account_reputation_history",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("reputation_score", sa.Integer(), nullable=False),
        sa.Column("risk_level", _enum("reputationrisklevel"), nullable=False),
        sa.Column("avg_depth", sa.Float(), nullable=False),
        sa.Column("reply_speed_dist", sa.Text(), nullable=True),
        sa.Column("human_ratio", sa.Float(), nullable=False),
        sa.Column("ignored_ratio", sa.Float(), nullable=False),
        sa.Column("policy_blocks", sa.Integer(), nullable=False),
        sa.Column("safety_triggers", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_account_reputation_history_id", "account_reputation_history", ["id"])
    op.create_index("ix_account_reputation_history_account_id", "account_reputation_history", ["account_id"])
    op.create_index("ix_account_reputation_history_date", "account_reputation_history", ["date"])

    op.create_table(
        "activity_events",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_activity_events_id", "activity_events", ["id"])
    op.create_index("ix_activity_events_account_id", "activity_events", ["account_id"])

    op.create_table(
        "last_processed_events",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("last_timestamp", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_last_processed_events_id", "last_processed_events", ["id"])
    op.create_index("ix_last_processed_events_account_id", "last_processed_events", ["account_id"], unique=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("ig_id", sa.String(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("is_paused", sa.Boolean(), nullable=False),
        sa.Column("last_reply_status", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Index("idx_users_account_ig", "account_id", "ig_id"),
    )
    op.create_index("ix_users_id", "users", ["id"])
    op.create_index("ix_users_account_id", "users", ["account_id"])
    op.create_index("ix_users_ig_id", "users", ["ig_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("direction", _enum("messagedirection"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("mid", sa.String(), nullable=True),
        sa.Index("idx_messages_account_created", "account_id", "timestamp"),
        sa.Index("idx_messages_account_user", "account_id", "user_id"),
    )
    op.create_index("ix_messages_id", "messages", ["id"])
    op.create_index("ix_messages_account_id", "messages", ["account_id"])
    op.create_index("ix_messages_mid", "messages", ["mid"])

    op.create_table(
        "auto_replies",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("keyword", sa.String(), nullable=False),
        sa.Column("response", sa.Text(), nullable=False),
        sa.Column("match_type", _enum("matchtype"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Index("idx_autoreplies_account_keyword", "account_id", "keyword"),
    )
    op.create_index("ix_auto_replies_id", "auto_replies", ["id"])
    op.create_index("ix_auto_replies_account_id", "auto_replies", ["account_id"])
    op.create_index("ix_auto_replies_keyword", "auto_replies", ["keyword"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("last_interaction", sa.DateTime(), nullable=False),
        sa.Column("msg_count_user", sa.Integer(), nullable=False),
        sa.Column("msg_count_bot", sa.Integer(), nullable=False),
        sa.Column("quality_score", _enum("conversationquality"), nullable=False),
        sa.Index("idx_conversations_account_last_interaction", "account_id", "last_interaction"),
    )
    op.create_index("ix_conversations_id", "conversations", ["id"])
    op.create_index("ix_conversations_account_id", "conversations", ["account_id"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(), primary_key=True, nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
    )

    op.create_table(
        "logs",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_logs_id", "logs", ["id"])
    op.create_index("ix_logs_account_id", "logs", ["account_id"])

    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("role", _enum("adminrole"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_admin_users_id", "admin_users", ["id"])
    op.create_index("ix_admin_users_account_id", "admin_users", ["account_id"])
    op.create_index("ix_admin_users_telegram_id", "admin_users", ["telegram_id"], unique=True)

    op.create_table(
        "admin_logs",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin_users.id"), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_admin_logs_id", "admin_logs", ["id"])
    op.create_index("ix_admin_logs_account_id", "admin_logs", ["account_id"])

    op.create_table(
        "tokens",
        sa.Column("service", sa.String(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_tokens_account_id", "tokens", ["account_id"])

    op.create_table(
        "daily_stats",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("new_users", sa.Integer(), nullable=False),
        sa.Column("auto_replies", sa.Integer(), nullable=False),
        sa.Column("human_replies", sa.Integer(), nullable=False),
        sa.Column("ignored_messages", sa.Integer(), nullable=False),
        sa.Column("top_keywords", sa.Text(), nullable=True),
    )
    op.create_index("ix_daily_stats_id", "daily_stats", ["id"])
    op.create_index("ix_daily_stats_account_id", "daily_stats", ["account_id"])
    op.create_index("ix_daily_stats_date", "daily_stats", ["date"])


def downgrade() -> None:
    op.drop_table("daily_stats")
    op.drop_table("tokens")
    op.drop_table("admin_logs")
    op.drop_table("admin_users")
    op.drop_table("logs")
    op.drop_table("settings")
    op.drop_table("conversations")
    op.drop_table("auto_replies")
    op.drop_table("messages")
    op.drop_table("users")
    op.drop_table("last_processed_events")
    op.drop_table("activity_events")
    op.drop_table("account_reputation_history")
    op.drop_table("accounts")
    op.drop_table("subscription_plans")

    bind = op.get_bind()
    for name in reversed(tuple(_ENUMS)):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)

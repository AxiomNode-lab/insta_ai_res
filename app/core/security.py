import hashlib
import hmac
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.models.all_models import AdminUser, AdminRole, AdminLog

import base64
from cryptography.fernet import Fernet
from app.core.config import settings

def get_account_fernet(account_id: int, secret_key: str | None = None) -> Fernet:
    """
    Derives a unique encryption key for each account using HMAC-SHA256.
    Key = Base64(HMAC(SECRET_KEY, account_id))
    """
    key_material = hmac.new(
        key=(secret_key or settings.SECRET_KEY).encode('utf-8'),
        msg=str(account_id).encode('utf-8'),
        digestmod=hashlib.sha256
    ).digest()
    
    # Fernet requires 32-bit url-safe base64 key
    final_key = base64.urlsafe_b64encode(key_material)
    return Fernet(final_key)

def encrypt_token(account_id: int, token: str) -> str:
    """
    Encrypts access token using account-specific key.
    """
    f = get_account_fernet(account_id)
    return f.encrypt(token.encode('utf-8')).decode('utf-8')

def decrypt_token(account_id: int, encrypted_token: str) -> str:
    """
    Decrypts access token. Raises InvalidToken if failed.
    """
    last_error = None
    for secret_key in settings.encryption_keys:
        try:
            return get_account_fernet(account_id, secret_key).decrypt(
                encrypted_token.encode("utf-8")
            ).decode("utf-8")
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise ValueError("No encryption keys configured")

def verify_meta_signature(payload: bytes, signature_header: str | None) -> bool:
    """
    Verifies the X-Hub-Signature-256 header sent by Meta Webhooks.
    """
    if not signature_header:
        return False
    
    # Meta sends signature as "sha256=<signature>"
    if not signature_header.startswith("sha256="):
        return False

    return any(
        hmac.compare_digest(
            "sha256="
            + hmac.new(
                key=secret.encode("utf-8"),
                msg=payload,
                digestmod=hashlib.sha256,
            ).hexdigest(),
            signature_header,
        )
        for secret in settings.meta_app_secrets
    )

def generate_appsecret_proof(access_token: str) -> str:
    """
    Generates appsecret_proof for Graph API calls.
    HMAC_SHA256(access_token, app_secret)
    """
    return hmac.new(
        key=settings.META_APP_SECRET.encode('utf-8'),
        msg=access_token.encode('utf-8'),
        digestmod=hashlib.sha256
    ).hexdigest()

async def get_admin_role(session: AsyncSession, telegram_id: int) -> AdminRole | None:
    stmt = select(AdminUser).where(AdminUser.telegram_id == telegram_id)
    result = await session.execute(stmt)
    admin = result.scalar_one_or_none()
    if admin:
        return admin.role
    # Fallback for hardcoded ADMIN_IDS as OWNER if not in DB
    if telegram_id in settings.ADMIN_IDS:
        return AdminRole.OWNER
    return None

async def log_admin_action(session: AsyncSession, telegram_id: int, action: str, details: str = None):
    # First get admin user DB ID
    stmt = select(AdminUser).where(AdminUser.telegram_id == telegram_id)
    result = await session.execute(stmt)
    admin = result.scalar_one_or_none()
    
    if not admin:
        # Should not happen if check passed, but handle case
        return

    log = AdminLog(
        admin_id=admin.id,
        action=action,
        details=details
    )
    session.add(log)
    await session.commit()

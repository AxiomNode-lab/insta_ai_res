"""Re-encrypt stored account tokens with the current SECRET_KEY.

Run only after deploying both SECRET_KEY (new) and SECRET_KEY_PREVIOUS (old).
No token values are printed.
"""

import asyncio

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.security import decrypt_token, encrypt_token
from app.models.all_models import Account


async def rotate() -> int:
    if not settings.SECRET_KEY_PREVIOUS:
        raise RuntimeError("SECRET_KEY_PREVIOUS is required for encryption-key rotation")
    if settings.SECRET_KEY == settings.SECRET_KEY_PREVIOUS:
        raise RuntimeError("Current and previous encryption keys must differ")

    async with AsyncSessionLocal() as session:
        accounts = (await session.execute(select(Account))).scalars().all()
        for account in accounts:
            plaintext = decrypt_token(account.id, account.access_token)
            account.access_token = encrypt_token(account.id, plaintext)
        await session.commit()
        return len(accounts)


if __name__ == "__main__":
    rotated = asyncio.run(rotate())
    print(f"Rotated encrypted tokens: {rotated}")

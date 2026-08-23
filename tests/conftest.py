import os


TEST_ENV = {
    "PROJECT_NAME": "IG Reply Desk Test",
    "ENV": "test",
    "SECRET_KEY": "test-secret-key-not-for-production",
    "POSTGRES_SERVER": "localhost",
    "POSTGRES_USER": "test",
    "POSTGRES_PASSWORD": "test",
    "POSTGRES_DB": "test",
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
    "REDIS_URL": "redis://localhost:6379/15",
    "META_APP_SECRET": "meta-current-test-secret",
    "META_APP_SECRET_PREVIOUS": "meta-previous-test-secret",
    "META_VERIFY_TOKEN": "verify-test-token",
    "INSTAGRAM_ACCESS_TOKEN": "instagram-test-token",
    "INSTAGRAM_PAGE_ID": "page-test-id",
    "TELEGRAM_BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
    "ADMIN_IDS": "[123456789]",
    "OPS_API_TOKEN": "ops-test-token",
}

for key, value in TEST_ENV.items():
    os.environ.setdefault(key, value)

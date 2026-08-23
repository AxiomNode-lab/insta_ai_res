import contextvars
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import Request

from app.core.config import settings


request_id_context: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(access[_-]?token|app[_-]?secret|bot[_-]?token|authorization)([\"'=:\s]+)([^\s,;\"']+)"),
)


def redact_log_message(value: object) -> str:
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}{match.group(2) if match.lastindex and match.lastindex > 1 else ''}[REDACTED]", text)
    return text


def opaque_id(value: object) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = hashlib.sha256(f"{settings.SECRET_KEY}:{day}".encode("utf-8")).digest()
    return hmac.new(key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:12]


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_log_message(record.getMessage())
        record.args = ()
        record.request_id = request_id_context.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                "level": record.levelname,
                "logger": record.name,
                "request_id": getattr(record, "request_id", "-"),
                "message": record.getMessage(),
            },
            ensure_ascii=False,
        )


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def request_context_middleware(request: Request, call_next):
    # External request IDs can contain secrets or customer identifiers.
    request_id = uuid.uuid4().hex
    token = request_id_context.set(request_id)
    started = time.monotonic()
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logging.getLogger("http.request").info(
            "request method=%s path=%s status=%s duration_ms=%s",
            request.method,
            request.url.path,
            response.status_code,
            round((time.monotonic() - started) * 1000, 2),
        )
        return response
    finally:
        request_id_context.reset(token)

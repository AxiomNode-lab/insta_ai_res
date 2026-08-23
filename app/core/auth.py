import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from app.core.config import settings


@dataclass(frozen=True)
class OpsPrincipal:
    subject: str
    role: str = "operator"


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    return value.strip() or None


def _token_matches(candidate: str | None) -> bool:
    if not candidate:
        return False
    candidate_digest = hashlib.sha256(candidate.encode("utf-8")).digest()
    return any(
        hmac.compare_digest(
            candidate_digest,
            hashlib.sha256(expected.encode("utf-8")).digest(),
        )
        for expected in settings.ops_api_tokens
    )


async def require_ops_access(
    authorization: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> OpsPrincipal:
    candidate = _extract_bearer(authorization) or x_ops_token
    if not _token_matches(candidate):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Operator authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return OpsPrincipal(subject="ops-token")

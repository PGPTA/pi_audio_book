"""Thin auth helpers: bcrypt password check + signed session cookie."""
from __future__ import annotations

import time
from typing import Optional

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner


COOKIE_NAME = "audiorec_session"


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


class SessionSigner:
    def __init__(self, secret: str, lifetime_s: int) -> None:
        self._signer = TimestampSigner(secret)
        self._lifetime = lifetime_s

    def make_cookie(self, username: str) -> str:
        return self._signer.sign(username.encode("utf-8")).decode("utf-8")

    def verify_cookie(self, cookie: Optional[str]) -> Optional[str]:
        if not cookie:
            return None
        try:
            unsigned = self._signer.unsign(cookie, max_age=self._lifetime)
        except SignatureExpired:
            return None
        except BadSignature:
            return None
        return unsigned.decode("utf-8")

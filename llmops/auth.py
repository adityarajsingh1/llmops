"""
Team API keys. The gateway only ever stores the SHA-256 of a key, so a leaked
gateway.yaml doesn't leak working keys. Make a new one with `llmops keys new TEAM`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable
from typing import Protocol

KEY_PREFIX = "llmops-"


class HasKeyHash(Protocol):
    key_sha256: str


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def bearer_token(header: str | None) -> str | None:
    """The key from an `Authorization: Bearer <key>` header, or None."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def find_owner(key: str, owners: Iterable[HasKeyHash]) -> HasKeyHash | None:
    """The team whose hash matches the key. Compares against every team in constant time."""
    digest = hash_key(key)
    found = None
    for owner in owners:
        if hmac.compare_digest(digest, owner.key_sha256):
            found = owner
    return found

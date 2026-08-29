"""Shared validation for the local read-only CDP probe identity."""

from __future__ import annotations

PROBE_TOKEN_ENV = "FIN_CDP_BRIDGE_TOKEN"
MIN_PROBE_TOKEN_CHARS = 24
MAX_PROBE_TOKEN_CHARS = 256


def probe_token_is_valid(raw_token: object) -> bool:
    """Validate shape only; never retain, log, or return the secret value."""

    return bool(
        isinstance(raw_token, str)
        and raw_token != "__default__"
        and MIN_PROBE_TOKEN_CHARS <= len(raw_token) <= MAX_PROBE_TOKEN_CHARS
        and raw_token.isprintable()
        and not any(character.isspace() for character in raw_token)
    )

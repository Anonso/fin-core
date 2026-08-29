"""Stable hash-based ID generation for cross-module consistency."""

import hashlib


def stable_id(*parts: str, prefix: str = "", digest_len: int = 12) -> str:
    """Generate a stable, deterministic ID from string parts.

    Uses MD5 for speed (not security-critical).

    Parameters
    ----------
    *parts : str
        Strings to join into the hashing input.
    prefix : str
        Optional prefix, e.g. ``"claim:"`` or ``"dynsig:"``.
    digest_len : int
        Number of hex characters to truncate (default 12, ~48 bits of entropy).

    Returns
    -------
    str
        Stable hash-based ID.

    Examples
    --------
    >>> stable_id("evidence_42", "company_mention", prefix="claim:")
    'claim:a1b2c3d4e5f6'
    """
    raw = "".join(parts)
    h = hashlib.md5(raw.encode()).hexdigest()[:digest_len]
    return f"{prefix}{h}"

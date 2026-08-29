"""LLM text processing utilities shared across claim/viewextraction."""


def truncate_for_llm(text: str, limit: int = 4000) -> str:
    """Truncate text for LLM API input limits.

    Uses character-based truncation (not token-based).
    """
    return text[:limit]


def strip_markdown_fences(text: str) -> str:
    """Strip surrounding markdown code fences (`` ```json ... ``` ``) from LLM output.

    Handles:
    - `` ```json`` / `` ```python`` / plain `` ``` `` fences
    - Leading and trailing backtick blocks
    """
    clean = text.strip()
    if clean.startswith("```"):
        lines = clean.split("\n")
        # Skip language tag on first line, drop trailing fence
        clean = "\n".join(lines[1:])
        if clean.endswith("```"):
            clean = clean[:-3].strip()
    return clean

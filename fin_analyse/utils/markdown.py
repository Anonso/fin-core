"""Markdown processing utilities."""

from __future__ import annotations

from typing import Any

import yaml


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from markdown text using yaml.safe_load.

    Handles inline lists, YAML block lists, nested structures, colons in
    values, and type coercion (bool / null / numbers) correctly.

    Parameters
    ----------
    text : str
        Raw markdown text, optionally starting with a ``---`` frontmatter block.

    Returns
    -------
    tuple[dict[str, Any], str]
        (metadata, body) where body is the text after the frontmatter block.
    """
    if not text.startswith("---"):
        return {}, text.strip()

    end = text.find("\n---", 3)
    if end == -1:
        return {}, text.strip()

    meta_block = text[3:end].strip()
    body = text[end + 4 :].strip()

    try:
        meta = yaml.safe_load(meta_block) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    return meta, body

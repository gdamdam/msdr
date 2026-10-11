"""Source-aware catalog language identities without rewriting source values.

Broadcast schedules use a mixture of full language names and compact codes.
Keep those imported strings on CatalogEntry; these helpers are only for
filtering and grouping equivalent values in the UI.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

_COMPOUND = re.compile(r"\s*[/,;+&]\s*")
_HFCC_LANGUAGES_PATH = Path(__file__).resolve().parent / "data" / "hfcc" / "languages.json"


@lru_cache(maxsize=1)
def _hfcc_language_names() -> dict[str, str]:
    """Read the bundled HFCC ISO-639-3 code table for legacy raw entries."""
    try:
        return json.loads(_HFCC_LANGUAGES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=1)
def _hfcc_name_identities() -> dict[str, tuple[str, str]]:
    """Index complete canonical names and their exact Latin-1 mojibake forms."""
    names: dict[str, tuple[str, str]] = {}
    for name in _hfcc_language_names().values():
        # One-character labels can collide with source language codes such as
        # EiBi's E, so only use full names as cross-source aliases.
        if len(name.strip()) < 2:
            continue
        identity = _key(name)
        names.setdefault(identity, (identity, name))
        try:
            mojibake = name.encode("utf-8").decode("latin1")
        except UnicodeEncodeError:
            continue
        names.setdefault(_key(mojibake), (identity, name))
    return names


def _key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


def _identity(source: str, token: str) -> tuple[str, str]:
    key = _key(token)
    known_name = _hfcc_name_identities().get(key)
    if known_name:
        return known_name
    if source.casefold() == "hfcc":
        name = _hfcc_language_names().get(token.strip().title())
        if name:
            return _key(name), name
    if key == "english" or key == "eng":
        return "english", "English"
    # EiBi's published language column uses one-letter codes. Restrict this
    # alias to that source so an arbitrary one-letter value elsewhere remains
    # searchable as its own source value.
    if source.casefold() == "eibi" and key == "e":
        return "english", "English"
    return key, token.strip()


def language_parts(source: str, value: str | None) -> list[tuple[str, str]]:
    """Return canonical identity/display pairs, splitting known compound
    separators while retaining the original value on the catalog entry."""
    if not value or not value.strip():
        return []
    # A slash or comma can be part of a language's official name. Recognize a
    # complete bundled HFCC name (or its exact legacy mojibake form) before
    # treating those characters as compound-value separators.
    known_name = _hfcc_name_identities().get(_key(value))
    if known_name:
        return [known_name]
    return [_identity(source, token) for token in _COMPOUND.split(value.strip()) if token.strip()]


def language_matches(source: str, value: str | None, requested: str) -> bool:
    # Settings historically store EiBi's one-letter codes even though the UI
    # applies them across all selected sources.
    if requested.startswith("identity:"):
        wanted = requested.removeprefix("identity:")
    else:
        wanted = _identity("eibi", requested)[0]
    return any(identity == wanted for identity, _display in language_parts(source, value))

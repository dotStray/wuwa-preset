"""Turning display names into ids and ids into display names."""

from __future__ import annotations

import re
import unicodedata

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def clean_name(text: str) -> str:
    """A display name without the markup some sources leave in: ``LV.<unbreak>999</unbreak>``."""
    text = re.sub(r"<[^<>]*>|\{[^{}]*\}", "", text)
    return re.sub(r"\s+", " ", text).strip()


def ascii_words(text: str) -> list[str]:
    """The words of ``text``, accents and apostrophes removed, other punctuation dropped."""
    text = re.sub(r"['\u2019`]", "", text)
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.findall(r"[A-Za-z0-9]+", plain)


def join_key(text: str) -> str:
    """The form two names are compared in: letters and digits only, lower case."""
    return "".join(ascii_words(text)).lower()


def pascal(text: str) -> str:
    """``"Lan Yan"`` → ``"LanYan"``; ``"Soldier 0 - Anby"`` → ``"Soldier0Anby"``. Existing capitals are kept."""
    return "".join(word[:1].upper() + word[1:] for word in ascii_words(text))


def split_camel(identifier: str) -> str:
    """``"GanyuTwilight"`` → ``"Ganyu Twilight"``; ``"SilverWolf999"`` → ``"Silver Wolf 999"``."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])", " ", identifier)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    return spaced.replace("_", " ").replace("-", " ").strip()


def is_valid_id(value: str) -> bool:
    # fullmatch: with match, "$" also matches before a final newline, and "Foo\n" passed (P9).
    return bool(ID_PATTERN.fullmatch(value))

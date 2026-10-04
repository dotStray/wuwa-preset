"""``config/<game>.json`` (how to build a game) and ``overrides/<game>.json`` (corrections).

Both are read strictly: a key the builder does not know is an error, because a misspelt
correction that is silently ignored is exactly the kind of fix that looks applied and is not.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from packbuilder.files import BuildError, read_json

CONFIG_KEYS = {"gameId", "displayName", "shortName", "importer", "disabledPrefix", "hashes", "roster", "portraits", "icon", "attributes"}
OVERRIDE_KEYS = {
    "notes",
    "join",
    "parents",
    "partOf",
    "displayNames",
    "aliases",
    "exclude",
    "ignoredHashes",
    "retired",
    "allowShrink",
}


@dataclass
class Overrides:
    join: dict[str, str] = field(default_factory=dict)
    parents: dict[str, str | None] = field(default_factory=dict)
    part_of: dict[str, str] = field(default_factory=dict)
    display_names: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)
    ignored_hashes: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    allow_shrink: list[str] = field(default_factory=list)


def load_config(path: pathlib.Path) -> dict:
    config = read_json(path)
    if not isinstance(config, dict):
        raise BuildError(f"{path} is missing or is not a JSON object.")
    unknown = sorted(set(config) - CONFIG_KEYS)
    if unknown:
        raise BuildError(f"{path}: unknown setting(s) {', '.join(unknown)}. Known: {', '.join(sorted(CONFIG_KEYS))}.")
    for required in ("gameId", "displayName", "hashes", "roster", "attributes"):
        if required not in config:
            raise BuildError(f"{path}: \"{required}\" is required.")
    return config


def load_overrides(path: pathlib.Path) -> Overrides:
    raw = read_json(path, {})
    if not isinstance(raw, dict):
        raise BuildError(f"{path} must be a JSON object.")
    if "outfitImages" in raw:
        raise BuildError(
            f"{path}: \"outfitImages\" is no longer used: the character list names every outfit by its number now, "
            "and the ledger remembers which folder is whose. Take it out; to tie an outfit to a folder, use \"join\"."
        )
    unknown = sorted(set(raw) - OVERRIDE_KEYS)
    if unknown:
        raise BuildError(f"{path}: unknown key(s) {', '.join(unknown)}. Known: {', '.join(sorted(OVERRIDE_KEYS))}.")

    def mapping(key: str, allow_null: bool = False) -> dict:
        value = raw.get(key, {})
        if not isinstance(value, dict) or not all(
            isinstance(v, str) or (allow_null and v is None) for v in value.values()
        ):
            raise BuildError(f"{path}: \"{key}\" must map names to {'a name or null' if allow_null else 'a name'}.")
        return dict(value)

    def names(key: str) -> list[str]:
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise BuildError(f"{path}: \"{key}\" must be a list of names.")
        return list(value)

    aliases = raw.get("aliases", {})
    if not isinstance(aliases, dict) or not all(isinstance(v, list) and all(isinstance(a, str) for a in v) for v in aliases.values()):
        raise BuildError(f"{path}: \"aliases\" must map a character to a list of names.")

    return Overrides(
        join=mapping("join"),
        parents=mapping("parents", allow_null=True),
        part_of=mapping("partOf"),
        display_names=mapping("displayNames"),
        aliases={k: list(v) for k, v in aliases.items()},
        exclude=names("exclude"),
        ignored_hashes=[h.lower() for h in names("ignoredHashes")],
        retired=names("retired"),
        allow_shrink=names("allowShrink"),
    )

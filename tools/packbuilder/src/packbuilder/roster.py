"""The roster stage: who is in the game, and what outfits they have, from nanoka.cc.

The list is read into one shape and saved as ``upstream/<game>/roster.json``, so the build can run
from that file alone when the source is down, and a diff of it shows exactly what the source
changed. Nothing here knows about hashes; the two stages meet only in :mod:`packbuilder.assemble`,
by name. **The list decides who is in the pack**: a character is in from the day nanoka lists them,
with or without hashes, and a hash entry never makes a character of its own.

nanoka's pages load their data from ``static.nanoka.cc`` as plain JSON, and this reads the same files:

- ``manifest.json`` — each game's newest data version (``latest``, the beta's while there is one) and
  the live game's (``live``).
- ``<game>/<version>/character.json`` — every character by number: English name, element, weapon,
  rank, and the path of its 256-pixel face icon in the game's files.
- ``<game>/<version>/en/character/<number>.json`` — one character's page, with its outfits (``skin``).

It is not a published API: a file that is missing or not in this shape is a fetch error, and the
build keeps the last run's list.

- **Released and unreleased alike.** The newest version's list is read; a character the live game
  does not have yet is in the pack, waiting for hashes like any other, with nothing to mark it out.
- **Nothing seen is dropped silently.** An entry that is not added — no name yet — is in
  ``left_out`` with why, and the build writes that to ``reports/<game>/left-out.md``.
- **A character's page is read only when it may have changed**: when the data version or the
  character's list entry changed since the last run. When a page cannot be read, what the last run
  found stays.
- **Outfits are every look but the character's own** (the one nanoka sorts first). An outfit has no
  face icon in nanoka, only its full card; the pack shows the card's top square.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from packbuilder.http import Fetcher, FetchError
from packbuilder.names import clean_name, join_key

# Part of every character's checksum: change it whenever what is taken from a character's page
# changes (a picture rule, a new field), so the next build reads every page again rather than
# keeping what an older rule found.
READER = 1

@dataclass
class Outfit:
    key: str
    name: str | None
    image: str | None
    crop: str | None = None  # how to cut `image` into a portrait, when not as the game's setting says


@dataclass
class Character:
    key: str
    name: str
    join_keys: list[str]
    word_keys: list[str] = field(default_factory=list)
    attributes: dict = field(default_factory=dict)
    image: str | None = None
    outfits: list[Outfit] = field(default_factory=list)
    family: str | None = None  # the key every form of one character shares (the Traveler, once per element)
    branch: str = "release"  # "beta": only the game's beta has it so far
    source_hash: str | None = None  # what the source said of this character when its page was last read
    left_out: list["LeftOut"] = field(default_factory=list)  # what its page lists that is not an outfit

    def to_json(self) -> dict:
        payload = {"key": self.key, "name": self.name, "joinKeys": self.join_keys}
        if self.word_keys:
            payload["wordKeys"] = self.word_keys
        if self.family:
            payload["family"] = self.family
        if self.branch != "release":
            payload["branch"] = self.branch
        if self.attributes:
            payload["attributes"] = self.attributes
        if self.image:
            payload["image"] = self.image
        if self.outfits:
            payload["outfits"] = [
                {k: v for k, v in (("key", o.key), ("name", o.name), ("image", o.image), ("imageCrop", o.crop)) if v is not None}
                for o in self.outfits
            ]
        if self.source_hash:
            payload["sourceHash"] = self.source_hash
        if self.left_out:
            payload["leftOut"] = [item.to_json() for item in self.left_out]
        return payload

    @staticmethod
    def from_json(payload: dict) -> "Character":
        return Character(
            key=payload["key"],
            name=payload["name"],
            join_keys=list(payload.get("joinKeys", [])),
            word_keys=list(payload.get("wordKeys", [])),
            attributes=dict(payload.get("attributes", {})),
            image=payload.get("image"),
            outfits=[Outfit(o["key"], o.get("name"), o.get("image"), o.get("imageCrop")) for o in payload.get("outfits", [])],
            family=payload.get("family"),
            branch=payload.get("branch", "release"),
            source_hash=payload.get("sourceHash"),
            left_out=[LeftOut.from_json(item) for item in payload.get("leftOut", [])],
        )


@dataclass
class LeftOut:
    """An entry the source has that is not in the pack, and why."""

    key: str
    name: str
    reason: str

    def to_json(self) -> dict:
        return {"key": self.key, "name": self.name, "reason": self.reason}

    @staticmethod
    def from_json(payload: dict) -> "LeftOut":
        return LeftOut(str(payload.get("key", "")), str(payload.get("name", "")), str(payload.get("reason", "")))


@dataclass
class Roster:
    characters: list[Character]
    left_out: list[LeftOut] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def keys_for(name: str, *codes: str) -> dict:
    """The forms of a character's name a hash folder might use.

    ``join_keys`` are the whole name and the game's own code for the character; ``word_keys``
    are the last and first words of a longer name ("Heizou" for "Shikanoin Heizou"). The
    builder tries every character's whole names before anyone's single words, so a word can
    never take a folder that belongs to someone else's whole name.
    """
    words = [join_key(w) for w in name.replace("•", " ").replace("&", " ").split()]
    words = [w for w in words if w]
    exact = _distinct([join_key(name), *(join_key(c) for c in codes if c)])
    loose = _distinct([words[-1], words[0]]) if len(words) > 1 else []
    return {"join_keys": exact, "word_keys": [w for w in loose if w not in exact]}


def _distinct(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


# ---- nanoka. --------------------------------------------------------------------------------------


def read(fetcher: Fetcher, previous: list[Character], settings: dict) -> Roster:
    """Every character in the game, from nanoka, using ``previous`` for what has not changed.

    ``settings`` is ``config`` → ``roster``: ``dataUrl`` (``https://static.nanoka.cc``), ``game``
    (nanoka's key for it, ``ww``) and ``imagesUrl`` (where its pictures are). Raises
    :class:`FetchError` when the list cannot be read: the caller then keeps the whole last list.
    """
    data_url = str(settings["dataUrl"]).rstrip("/")
    game = str(settings["game"])
    images = str(settings["imagesUrl"]).rstrip("/")
    known = {c.key: c for c in previous}
    result = Roster(characters=[])

    manifest = fetcher.get_json(f"{data_url}/manifest.json")
    versions = manifest.get(game) if isinstance(manifest, dict) else None
    latest = str(versions.get("latest") or "") if isinstance(versions, dict) else ""
    live = str(versions.get("live") or "") if isinstance(versions, dict) else ""
    if not latest:
        raise FetchError(f"{data_url}/manifest.json: no version for '{game}' in it.")

    listing = _list(fetcher, f"{data_url}/{game}/{latest}/character.json")
    released = set(listing)
    if live and live != latest:
        try:
            released = set(_list(fetcher, f"{data_url}/{game}/{live}/character.json"))
        except FetchError as error:
            result.warnings.append(f"The live game's list was not read ({error}); every character is taken as released.")

    for number, entry in sorted(listing.items(), key=lambda item: (len(item[0]), item[0])):
        key = f"avatar:{number}"
        name = clean_name(str(entry.get("en") or ""))
        before = known.get(key)
        if not name and before:
            name = before.name  # a name found once is kept: the source's text can go missing for a while
        if not name:
            result.left_out.append(LeftOut(key, f"character {number}", "it has no name in the source yet; it is added the week it gets one"))
            continue
        character = Character(
            key=key,
            name=name,
            **keys_for(name),
            attributes={k: v for k, v in (("element", entry.get("element")), ("weaponClass", entry.get("weapon")), ("rarity", entry.get("rank"))) if v is not None},
            image=asset_url(images, entry.get("icon")),
            branch="release" if number in released else "beta",
        )
        stamp = f"{latest}:r{READER}:{hashlib.sha256(json.dumps(entry, sort_keys=True).encode('utf-8')).hexdigest()[:16]}"
        if before and before.source_hash == stamp:
            character.outfits, character.source_hash = list(before.outfits), stamp
        else:
            try:
                page = fetcher.get_json(f"{data_url}/{game}/{latest}/en/character/{number}.json")
                character.outfits = outfits(page, images)
                character.source_hash = stamp
            except FetchError as error:
                kept = "last run's outfits are kept" if before else "it is in without outfits until it can be"
                result.warnings.append(f"{name}'s page was not read ({error}); {kept}.")
                if before:
                    character.outfits = list(before.outfits)
        result.characters.append(character)

    result.characters.sort(key=lambda c: c.key)
    return result


def _list(fetcher: Fetcher, url: str) -> dict[str, dict]:
    listing = fetcher.get_json(url)
    if not isinstance(listing, dict) or not listing:
        raise FetchError(f"{url}: the character list was empty or not a list of characters.")
    entries = {str(k): v for k, v in listing.items() if isinstance(v, dict) and str(k).isdigit()}
    if not entries:
        raise FetchError(f"{url}: no character in it.")
    return entries


def outfits(page: object, images: str) -> list[Outfit]:
    """Every look on a character's page but its own: the one nanoka sorts first."""
    skins = page.get("skin") if isinstance(page, dict) else None
    if not isinstance(skins, dict):
        raise FetchError("the character page has no outfits in it (no 'skin').")
    looks = [s for s in skins.values() if isinstance(s, dict) and s.get("id") is not None]
    looks.sort(key=lambda s: (s.get("sort_index") if isinstance(s.get("sort_index"), int) else 0, str(s["id"])))
    return sorted(
        (
            Outfit(f"skin:{s['id']}", clean_name(str(s.get("title_name") or "")) or None, asset_url(images, s.get("background")), crop="top-square")
            for s in looks[1:]
        ),
        key=lambda o: o.key,
    )


GAME_PREFIX = "/Game/Aki/UI/"


def asset_url(images: str, path: object) -> str | None:
    """A picture's address on nanoka, from its path in the game's files, the way nanoka's own pages make it.

    ``/Game/Aki/UI/UIResources/…/T_IconRoleHead256_7_UI.T_IconRoleHead256_7_UI`` is
    ``<images>/UIResources/…/T_IconRoleHead256_7_UI.webp``. Anything else is no picture: an address
    elsewhere is never followed.
    """
    if not isinstance(path, str) or not path.startswith(GAME_PREFIX):
        return None
    rest = path[len(GAME_PREFIX):].split(".", 1)[0]
    return f"{images}/{rest}.webp" if rest and ".." not in rest else None

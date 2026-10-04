"""The hand-kept ``manual/<game>/`` folder: hashes, characters and portraits added by a person.

Everything here is a plain file that can be added from GitHub's website:

- ``hashes/<Name>.txt`` — hashes in any form people paste: one per line, ``ib 1575ec63``,
  or whole ``[TextureOverride…]`` sections copied out of a mod's ``.ini``.
- ``hashes/<Name>.json`` — a ``hash.json`` in the GIMI-style asset repositories' format.
- ``hashes/replace/<Name>.txt|.json`` — the same, but standing in for the hash source's hashes
  of that character entirely, for as long as the file is there: for when upstream's are wrong.
- ``images/<Name>.png|.jpg|.jpeg|.webp`` — a portrait for a character the sources have no picture of.
  Once a source has one, the source's is used and this file is reported as no longer used: nothing to
  remember to delete (the user's choice, 2026-09-27).
- ``images/keep/<Name>.png|…`` — a portrait that beats any downloaded one, for as long as it is there.
- ``images/_game.png|.jpg|.jpeg|.webp`` — the game's icon, beside its name in XXSM. It beats the
  one the build downloads (``config`` → ``icon``), for as long as the file is here.
- ``characters.json`` — characters the character lists do not have yet, when a hashes file
  alone is not enough (a display name with spaces, or an outfit of someone).

``<Name>`` is matched against the pack's characters ignoring case, then by its letters alone
("Luuk Herssen" finds ``LuukHerssen``), then against display names. A name that finds nobody becomes a
new character, and the build report says so, so that a typo is visible rather than silent.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field

from packbuilder.files import BuildError, read_json
from packbuilder.hashes import HEX, entries as upstream_entries

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

# The picture in images/ that is the game's icon rather than a character's portrait.
GAME_ICON = "_game"

# The last meaningful word of a section name says which buffer or texture a hash is for.
# The same markers modders use: `[TextureOverrideAugustaIB]`, `…HeadDiffuse`, `…Position`. A WWMI
# mod's `[TextureOverrideComponent0]` holds the model's main hash, which the pack publishes as `ib`.
SECTION_MARKERS = {
    "component": "ib",
    "ib": "ib",
    "index": "ib",
    "indexbuffer": "ib",
    "position": "position_vb",
    "pos": "position_vb",
    "blend": "blend_vb",
    "texcoord": "texcoord_vb",
    "draw": "draw_vb",
    "vb": "draw_vb",
    "diffuse": "texture",
    "lightmap": "texture",
    "normalmap": "texture",
    "shadowramp": "texture",
    "metalmap": "texture",
    "materialmap": "texture",
    "texture": "texture",
}

# Words a person might put before a hash on a line of its own.
LINE_KINDS = {
    **{k: v for k, v in SECTION_MARKERS.items()},
    "position_vb": "position_vb",
    "blend_vb": "blend_vb",
    "texcoord_vb": "texcoord_vb",
    "draw_vb": "draw_vb",
    "root_vs": "root_vs",
    "unknown": "unknown",
}

SECTION_PREFIXES = ("textureoverride", "shaderoverride", "resource", "commandlist", "customshader")


@dataclass
class ManualCharacter:
    name: str  # as the file or characters.json gave it
    display_name: str | None = None
    internal_name: str | None = None
    outfit_of: str | None = None
    source: str = ""


@dataclass
class Manual:
    hashes: dict[str, list[dict]] = field(default_factory=dict)  # name as written → entries (variant filled later)
    hash_sources: dict[str, str] = field(default_factory=dict)
    replacements: dict[str, list[dict]] = field(default_factory=dict)  # hashes/replace/: instead of upstream's
    replacement_sources: dict[str, str] = field(default_factory=dict)
    images: dict[str, pathlib.Path] = field(default_factory=dict)
    kept_images: set[str] = field(default_factory=set)  # names whose picture is in images/keep/: used whatever the source has
    game_icon: pathlib.Path | None = None
    characters: list[ManualCharacter] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def read(folder: pathlib.Path) -> Manual:
    manual = Manual()
    if not folder.is_dir():
        return manual

    _read_hashes(folder, "hashes", manual.hashes, manual.hash_sources, manual.problems)
    _read_hashes(folder, "hashes/replace", manual.replacements, manual.replacement_sources, manual.problems)

    for path in sorted((folder / "images").glob("*")) if (folder / "images").is_dir() else []:
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            if path.stem.lower() == GAME_ICON:
                manual.game_icon = path
            else:
                manual.images[path.stem] = path

    # images/keep/ after images/, so a character with a picture in both uses the kept one.
    keep = folder / "images" / "keep"
    for path in sorted(keep.glob("*")) if keep.is_dir() else []:
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            manual.images[path.stem] = path
            manual.kept_images.add(path.stem)

    listed = read_json(folder / "characters.json", [])
    if not isinstance(listed, list):
        raise BuildError(f"manual/{folder.name}/characters.json must be a list, like [{{\"name\": \"Luuk Herssen\"}}].")
    for number, item in enumerate(listed, start=1):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            manual.problems.append(f"manual/{folder.name}/characters.json: entry {number} has no \"name\".")
            continue
        manual.characters.append(
            ManualCharacter(
                name=str(item["name"]).strip(),
                display_name=str(item.get("displayName") or item["name"]).strip(),
                internal_name=(str(item["id"]).strip() if item.get("id") else None),
                outfit_of=(str(item["outfitOf"]).strip() if item.get("outfitOf") else None),
                source=f"manual/{folder.name}/characters.json",
            )
        )
    return manual


def _read_hashes(folder: pathlib.Path, where: str, into: dict, sources: dict, problems: list[str]) -> None:
    for path in sorted((folder / where).glob("*")) if (folder / where).is_dir() else []:
        if not path.is_file() or path.name.startswith(".") or path.name.lower() == "readme.md":
            continue
        if path.is_symlink():
            problems.append(f"manual/{folder.name}/{where}/{path.name} is a link, and a link is not read. Put the file itself there.")
            continue
        name = path.stem
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        if path.suffix.lower() == ".json":
            try:
                components = json.loads(text)
            except json.JSONDecodeError as error:
                problems.append(f"manual/{folder.name}/{where}/{path.name}: not valid JSON ({error.msg}, line {error.lineno}).")
                continue
            found = upstream_entries("", components if isinstance(components, list) else [])
        else:
            found = parse_text(text)
        if not found:
            problems.append(f"manual/{folder.name}/{where}/{path.name}: no hashes found in it.")
            continue
        into.setdefault(name, []).extend(found)
        sources[name] = f"manual/{folder.name}/{where}/{path.name}"


def parse_text(text: str, *, texture_override_only: bool = False) -> list[dict]:
    """Every hash in pasted text or an ``.ini``, with its kind where the text says it.

    A 16-digit hash is a shader hash, which only ever means ``root_vs``. An 8-digit hash with
    nothing to say what it is becomes ``unknown``, which the app scores like a ``draw_vb``.
    A bare token must contain a digit to count, so an ordinary word such as "deadbeef" in a
    comment is not taken for a hash.
    """
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    section: str | None = None

    def add(kind: str, value: str) -> None:
        value = value.lower()
        if len(value) == 16:
            kind = "root_vs"
        if (kind, value) not in seen:
            seen.add((kind, value))
            found.append({"variant": "", "component": "", "kind": kind, "hash": value})

    for raw in text.splitlines():
        line = re.split(r"(?:^|\s)(?:;|#|//)", raw, maxsplit=1)[0].strip()
        if not line:
            continue
        header = re.fullmatch(r"\[([^\]]+)\]", line)
        if header:
            section = header.group(1).strip()
            continue
        assignment = re.fullmatch(r"hash\s*=\s*([0-9A-Fa-f]{8}|[0-9A-Fa-f]{16})", line, flags=re.IGNORECASE)
        if assignment:
            if texture_override_only and not (section or "").lower().startswith("textureoverride"):
                continue
            add(section_kind(section) or "unknown", assignment.group(1))
            continue
        if texture_override_only:
            continue
        tokens = re.findall(r"[A-Za-z_]+|[0-9A-Fa-f]+", line)
        word = tokens[0].lower() if tokens else ""
        for token in re.findall(r"\b[0-9A-Fa-f]{8}\b|\b[0-9A-Fa-f]{16}\b", line):
            if not any(c.isdigit() for c in token) or not set(token.lower()) <= HEX:
                continue
            add(LINE_KINDS.get(word, section_kind(section) or "unknown"), token)
    return found


def section_kind(section: str | None) -> str | None:
    """The kind a section name implies, from its last meaningful word, or ``None``."""
    if not section:
        return None
    name = section
    for prefix in SECTION_PREFIXES:
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
            break
    tokens = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", name)
    for i in range(len(tokens) - 1, -1, -1):
        token = tokens[i].lower()
        if i > 0 and (tokens[i - 1].lower() + token) in SECTION_MARKERS:
            return SECTION_MARKERS[tokens[i - 1].lower() + token]
        if token in SECTION_MARKERS:
            return SECTION_MARKERS[token]
        if token.isdigit() or len(token) <= 2:
            continue
        return None
    return None

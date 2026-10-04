"""``packbuilder learn``: take a character's hashes out of a mod and keep them in ``manual/``.

For the weeks between a character's release and upstream publishing their hashes: a mod
for them already contains them. This reads every ``.ini`` in a mod folder or ``.zip``,
takes the ``hash =`` lines in ``[TextureOverride…]`` sections, and works out each one's kind
from the section name the way modders write them (``…BodyIB``, ``…HeadDiffuse``).

A mod also carries hashes that are not that character's: the shared shader, a weapon, a
part copied from another character. Every hash some *other* character in the pack already
has is left out and listed, and so is every shader hash, before anything is written.
Hashes are added to ``manual/<game>/hashes/<Character>.txt`` with a comment saying which mod
they came from; nothing already in that file is removed.
"""

from __future__ import annotations

import pathlib
import zipfile
from dataclasses import dataclass, field

from packbuilder.files import BuildError, read_json
from packbuilder.manual import parse_text
from packbuilder.names import is_valid_id

INI_LIMIT = 4 * 1024 * 1024


@dataclass
class Learned:
    target: pathlib.Path
    added: list[dict] = field(default_factory=list)
    already: int = 0
    elsewhere: dict[str, list[str]] = field(default_factory=dict)  # hash → other characters
    shaders: int = 0
    files: int = 0


def read_inis(source: pathlib.Path) -> list[tuple[str, str]]:
    """(name, text) for every ``.ini`` in a folder or zip, ``DISABLED`` ones included."""
    found: list[tuple[str, str]] = []
    if source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix.lower() == ".ini" and path.stat().st_size <= INI_LIMIT:
                found.append((path.relative_to(source).as_posix(), path.read_bytes().decode("utf-8-sig", errors="replace")))
    elif source.is_file() and source.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(source) as archive:
                for info in sorted(archive.infolist(), key=lambda i: i.filename):
                    if not info.is_dir() and info.filename.lower().endswith(".ini") and info.file_size <= INI_LIMIT:
                        found.append((info.filename, archive.read(info).decode("utf-8-sig", errors="replace")))
        except zipfile.BadZipFile as error:
            raise BuildError(f"{source} is not a readable zip: {error}.") from error
    else:
        raise BuildError(f"{source} is not a folder or a .zip. (.7z and .rar: extract it first and point at the folder.)")
    if not found:
        raise BuildError(f"No .ini files in {source}, so there are no hashes to take.")
    return found


def learn(manual_dir: pathlib.Path, pack_dir: pathlib.Path, character: str, source: pathlib.Path, *, dry_run: bool = False) -> Learned:
    if not is_valid_id(character):
        raise BuildError(f"'{character}' is not a character id (letters, digits, - and _). Use the name the pack uses, like LuukHerssen.")
    variants = read_json(pack_dir / "variants.json", []) or []
    known = {v["internalName"].lower(): v["internalName"] for v in variants}
    character = known.get(character.lower(), character)

    owners: dict[str, set[str]] = {}
    for entry in (read_json(pack_dir / "hashes.json", {}) or {}).get("entries", []):
        owners.setdefault(entry["hash"], set()).add(entry["variant"])
    family = {character.lower()} | {
        v["internalName"].lower()
        for v in variants
        if (v.get("baseCharacterId") or "").lower() == character.lower() or v["internalName"].lower() == character.lower()
    }
    parent = next((v.get("baseCharacterId") for v in variants if v["internalName"].lower() == character.lower()), None)
    if parent:
        family.add(parent.lower())

    target = manual_dir / "hashes" / f"{character}.txt"
    existing = parse_text(target.read_text(encoding="utf-8")) if target.is_file() else []
    have = {e["hash"] for e in existing}
    result = Learned(target)

    inis = read_inis(source)
    result.files = len(inis)
    for _, text in inis:
        for entry in parse_text(text, texture_override_only=True):
            value = entry["hash"]
            if entry["kind"] == "root_vs" or len(value) == 16:
                result.shaders += 1
                continue
            others = sorted(o for o in owners.get(value, set()) if o.lower() not in family)
            if others:
                result.elsewhere[value] = others
                continue
            if value in have or any(value == a["hash"] for a in result.added):
                result.already += 1
                continue
            result.added.append(entry)

    if result.added and not dry_run:
        lines = [] if target.is_file() else [f"# Hashes for {character}, added by hand. One per line: <kind> <hash>.", ""]
        lines.append(f"# from {source.name}")
        lines += [f"{e['kind']} {e['hash']}" for e in result.added]
        gap = "\n" if target.is_file() and target.stat().st_size > 0 else ""
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(gap + "\n".join(lines) + "\n")
    return result

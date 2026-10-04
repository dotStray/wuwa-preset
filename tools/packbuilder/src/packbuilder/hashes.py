"""The hash stage: Moonholder's Wuwa_Mod_Fixer, read as one folder per entry.

Moonholder/Wuwa_Mod_Fixer keeps every hash its fixer knows in one file, ``config.json``, which its
own CI writes from ``config.yml``. Each entry under ``characters`` is one model the game draws — a
character, an outfit, a part of one (Zani's upper body, Augusta's summoned sword), a weapon, a
glider — and holds:

- ``main_hashes``: ``[{"old": [...], "new": "…"}]`` — the model's hash in every game version the fixer
  has seen. It is the hash a mod's ``[TextureOverrideComponentN]`` sections match on, one per model,
  so it is published as ``ib``: alone it is enough to say whose a mod is, as an ``ib`` is in the other
  games.
- ``textures``: ``{"<current>": {"meta": {"id": <component>, "type": "D|N|L|M"}, "replace": [older],
  "derive": {"LOD": [...], …}}}`` — each texture's hash now, before, and at other graphics settings.

Everything else in an entry (``rules``, ``vg_remaps``, ``shapekey_fix``…) is how the fixer rewrites a
mod, not who a mod is for, and is not read. Old hashes are kept on purpose: mods made before a game
update still carry them.

A copy of the file is kept in ``upstream/<game>/hashes/``, exactly as Moonholder wrote it, with
``lock.json`` recording the commit and the file's git blob id, so a rebuild downloads it only when it
changed. If the repository disappeared tomorrow, the copy here is still a complete record of what it
last said.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
from dataclasses import dataclass

from packbuilder.files import read_json, write_json, swap_in
from packbuilder.http import Fetcher, FetchError

KINDS = {
    "ib": "ib",
    "position_vb": "position_vb",
    "blend_vb": "blend_vb",
    "texcoord_vb": "texcoord_vb",
    "draw_vb": "draw_vb",
    "root_vs": "root_vs",
}

HEX = set("0123456789abcdef")


@dataclass(frozen=True)
class Folder:
    """One entry of the hash source, in the shape of a ``hash.json``."""

    name: str  # the entry's own name: "AugustaSword"
    path: str  # where it is in the source, for messages: the same name
    components: list
    not_hashes: tuple = ()  # values the source gives as hashes that are not one, left out


def refresh(repo: str, path: str, destination: pathlib.Path, fetcher: Fetcher) -> str:
    """Brings ``destination`` up to date with ``path`` in ``repo``. Returns the commit it now matches."""
    commit_info = fetcher.get_json(f"https://api.github.com/repos/{repo}/commits/HEAD")
    commit = str(commit_info.get("sha", "")) if isinstance(commit_info, dict) else ""
    if not commit:
        raise FetchError(f"https://api.github.com/repos/{repo}/commits/HEAD: no commit in the answer.")

    tree = fetcher.get_json(f"https://api.github.com/repos/{repo}/git/trees/{commit}?recursive=1", fresh=False)
    if not isinstance(tree, dict) or tree.get("truncated"):
        raise FetchError(f"{repo}: GitHub returned an incomplete file list; the copy was left as it was.")
    sha = next((e.get("sha") for e in tree.get("tree", []) if e.get("type") == "blob" and e.get("path") == path), None)
    if not sha:
        raise FetchError(f"{repo}: no {path} at {commit[:12]}; the copy was left as it was.")

    name = path.rsplit("/", 1)[-1]
    lock = read_json(destination / "lock.json", {}) or {}
    existing = destination / name
    if lock.get("repo") == repo and lock.get("path") == path and lock.get("sha") == sha and existing.is_file():
        data = existing.read_bytes()
    else:
        data = fetcher.get(f"https://raw.githubusercontent.com/{repo}/{commit}/{path}", fresh=False)
        if _blob_sha(data) != sha:
            raise FetchError(f"{repo}/{path}: the download did not match GitHub's checksum.")
    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FetchError(f"{repo}/{path} at {commit[:12]} is not valid JSON ({error}); the copy was left as it was.") from error
    if not isinstance(parsed, dict) or not isinstance(parsed.get("characters"), dict) or not parsed["characters"]:
        raise FetchError(f"{repo}/{path} at {commit[:12]} has no \"characters\" in it any more; the copy was left as it was.")

    staging = destination.with_name(destination.name + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / name).write_bytes(data)
    write_json(staging / "lock.json", {"repo": repo, "commit": commit, "path": path, "sha": sha})
    swap_in(staging, destination)
    return commit


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def load(destination: pathlib.Path) -> tuple[list[Folder], dict]:
    """The saved copy: one folder per entry that holds at least one hash, and the lock."""
    lock = read_json(destination / "lock.json", {}) or {}
    path = str(lock.get("path", ""))
    file = destination / path.rsplit("/", 1)[-1] if path else None
    if file is None or not file.is_file():
        return [], lock
    try:
        data = json.loads(file.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return [], lock
    found = data.get("characters") if isinstance(data, dict) else None
    folders = []
    for name, entry in sorted((found or {}).items(), key=lambda item: item[0].lower()):
        if isinstance(entry, dict):
            found_components, bad = components(entry)
            folders.append(Folder(name, name, found_components, tuple(bad)))
    return folders, lock


# What Moonholder's one-letter texture types are, in the words the other games' files use.
TEXTURE_KINDS = {"D": "Diffuse", "N": "NormalMap", "L": "LightMap", "M": "MaterialMap"}


def components(entry: dict) -> tuple[list, list[str]]:
    """One Moonholder entry as ``hash.json`` components — one per main hash, then one holding its
    textures — and every value it gives as a hash that is not one (a typo: seven digits)."""
    result: list = []
    bad: list[str] = []

    def keep(value: object) -> bool:
        if isinstance(value, str) and clean(value):
            return True
        if isinstance(value, str) and value.strip():
            bad.append(value.strip())
        return False

    for main in entry.get("main_hashes") or []:
        if not isinstance(main, dict):
            continue
        for value in [main.get("new"), *(main.get("old") or [])]:
            if keep(value):
                result.append({"component_name": "", "ib": value})

    groups: dict[int, list] = {}
    textures = entry.get("textures")
    for current, texture in (textures.items() if isinstance(textures, dict) else []):
        texture = texture if isinstance(texture, dict) else {}
        meta = texture.get("meta") if isinstance(texture.get("meta"), dict) else {}
        try:
            slot = max(0, int(meta.get("id", 0)))
        except (TypeError, ValueError):
            slot = 0
        kind = TEXTURE_KINDS.get(str(meta.get("type", "")).upper(), "Unknown")
        values = [current, *(texture.get("replace") or [])]
        derived = texture.get("derive")
        for more in (derived.values() if isinstance(derived, dict) else []):
            values += more if isinstance(more, list) else []
        groups.setdefault(slot, []).extend([kind, ".dds", v] for v in values if keep(v))
    if groups:
        result.append({"component_name": "", "texture_hashes": [groups.get(slot, []) for slot in range(max(groups) + 1)]})
    return result, list(dict.fromkeys(bad))


def clean(value: object) -> str:
    """A hash in lower case, or ``""``: an empty string means absent, never "matches empty", and a
    hash is 8 hex digits (a buffer or texture) or 16 (a shader), nothing else."""
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    return value if len(value) in (8, 16) and set(value) <= HEX else ""


def entries(variant: str, components: list) -> list[dict]:
    """``hash.json``'s components as the pack's flat entries, duplicates dropped, order kept."""
    result: list[dict] = []
    seen: set[tuple] = set()
    for component in components:
        if not isinstance(component, dict):
            continue
        name = component.get("component_name") if isinstance(component.get("component_name"), str) else ""
        for field, kind in KINDS.items():
            value = clean(component.get(field))
            key = (name, kind, value)
            if value and key not in seen:
                seen.add(key)
                result.append({"variant": variant, "component": name, "kind": kind, "hash": value})
        for slot, group in enumerate(component.get("texture_hashes") or []):
            for texture in group if isinstance(group, list) else []:
                if not isinstance(texture, list) or len(texture) != 3:
                    continue
                value = clean(texture[2])
                texture_kind = texture[0] if isinstance(texture[0], str) and texture[0] else "Unknown"
                key = (name, "texture", value, texture_kind, slot)
                if value and key not in seen:
                    seen.add(key)
                    result.append(
                        {"variant": variant, "component": name, "kind": "texture", "hash": value, "textureKind": texture_kind, "slot": slot}
                    )
    return result

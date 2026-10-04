"""Two kinds of check, both of which stop a build before anything is published.

``validate`` — is this a well-formed pack? The rules of ``PRESET_SCHEMA.md``, checked
here independently of XXSM (this repository uses nothing from it). XXSM's side checks the
published packs with its own loader, so a disagreement between the two shows up there.

``guard`` — does this pack look like a sensible next version of the last one? Nobody reviews
a weekly run by hand, so the run itself refuses what a person would have caught: a
character that disappeared, or one that lost most of its hashes. Either is almost always a
broken source rather than a real change, and publishing it would take a working character
away from everyone. A real removal is written into ``overrides/<game>.json`` first.
"""

from __future__ import annotations

import re

from packbuilder.names import is_valid_id

KINDS = {"ib", "position_vb", "blend_vb", "texcoord_vb", "draw_vb", "root_vs", "texture", "unknown"}
HASH = re.compile(r"^(?:[0-9a-f]{8}|[0-9a-f]{16})$")
MAX_IMAGE_BYTES = 200 * 1024


def validate(game: dict, variants: list[dict], hashes: dict, image_sizes: dict[str, int]) -> list[str]:
    errors: list[str] = []
    ids: dict[str, str] = {}
    for variant in variants:
        name = variant.get("internalName", "")
        if not is_valid_id(name):
            errors.append(f"'{name}' is not a valid internal name (letters, digits, - and _ only).")
        if name.lower() in ids:
            errors.append(f"'{name}' and '{ids[name.lower()]}' are the same name ignoring capitals.")
        ids[name.lower()] = name
        if not str(variant.get("displayName", "")).strip():
            errors.append(f"'{name}' has no display name.")

    families: dict[str, list[dict]] = {}
    for variant in variants:
        parent = variant.get("baseCharacterId")
        name = variant["internalName"]
        if parent is None:
            families.setdefault(name.lower(), []).append(variant)
            continue
        if parent.lower() == name.lower():
            errors.append(f"'{name}' is its own base character.")
        elif parent.lower() not in ids:
            errors.append(f"'{name}' is an outfit of '{parent}', which is not in the pack.")
        else:
            base = next(v for v in variants if v["internalName"].lower() == parent.lower())
            if base.get("baseCharacterId"):
                errors.append(f"'{name}' is an outfit of '{parent}', which is itself an outfit. Families have one level.")
            families.setdefault(parent.lower(), []).append(variant)
    for key, members in families.items():
        defaults = [m["internalName"] for m in members if m.get("isDefaultVariant")]
        if len(defaults) != 1:
            errors.append(f"The family of '{ids.get(key, key)}' has {len(defaults)} default variants; it needs exactly one.")

    attributes = game.get("attributes", {})
    for variant in variants:
        for attribute, value in (variant.get("attributes") or {}).items():
            spec = attributes.get(attribute)
            if spec is None:
                errors.append(f"'{variant['internalName']}' has attribute '{attribute}', which game.json does not declare.")
                continue
            if spec.get("kind") == "number":
                continue
            allowed = {v["id"] for v in spec.get("values", [])}
            for item in value if isinstance(value, list) else [value]:
                if item not in allowed:
                    errors.append(f"'{variant['internalName']}' has {attribute} '{item}', which game.json does not declare.")
        image = variant.get("image")
        if image:
            size = image_sizes.get(image)
            if size is None:
                errors.append(f"'{variant['internalName']}' points at {image}, which is not in the pack.")
            elif size > MAX_IMAGE_BYTES:
                errors.append(f"{image} is {size // 1024} KB; the most a pack picture may be is {MAX_IMAGE_BYTES // 1024} KB.")

    with_hashes: set[str] = set()
    for number, entry in enumerate(hashes.get("entries", []), start=1):
        name = str(entry.get("variant", ""))
        if name.lower() not in ids:
            errors.append(f"Hash entry {number} is for '{name}', which is not in the pack.")
        if entry.get("kind") not in KINDS:
            errors.append(f"Hash entry {number} ({name}) has kind '{entry.get('kind')}'.")
        if not HASH.match(str(entry.get("hash", ""))):
            errors.append(f"Hash entry {number} ({name}) has '{entry.get('hash')}', which is not an 8- or 16-digit lower-case hex hash.")
        with_hashes.add(name.lower())
    for variant in variants:
        if variant.get("hashesPending") and variant["internalName"].lower() in with_hashes:
            errors.append(f"'{variant['internalName']}' is marked hashesPending but has hashes.")
    return errors


def guard(previous_variants: list[dict], previous_hashes: dict, variants: list[dict], hashes: dict, retired: list[str], allow_shrink: list[str]) -> list[str]:
    if not previous_variants:
        return []
    problems: list[str] = []
    now = {v["internalName"].lower() for v in variants}
    retired_keys = {r.lower() for r in retired}
    gone = [v["internalName"] for v in previous_variants if v["internalName"].lower() not in now and v["internalName"].lower() not in retired_keys]
    if gone:
        problems.append(
            f"{len(gone)} character(s) that are in the published pack would disappear: {', '.join(sorted(gone))}. "
            "That is almost always a source having a bad day, so nothing was published. If they really are gone, "
            "add them to \"retired\" in overrides/<game>.json."
        )

    def counts(payload: dict) -> dict[str, int]:
        result: dict[str, int] = {}
        for entry in payload.get("entries", []):
            key = str(entry.get("variant", "")).lower()
            result[key] = result.get(key, 0) + 1
        return result

    before, after = counts(previous_hashes), counts(hashes)
    shrink_ok = {a.lower() for a in allow_shrink}
    spelled = {v["internalName"].lower(): v["internalName"] for v in variants}
    shrunk = [
        f"{spelled[name]} ({before[name]} → {after.get(name, 0)})"
        for name in sorted(before)
        if name in now and before[name] >= 8 and after.get(name, 0) < before[name] / 2 and name not in shrink_ok
    ]
    if shrunk:
        problems.append(
            f"{len(shrunk)} character(s) would lose more than half their hashes: {', '.join(shrunk)}. "
            "Nothing was published. If upstream really did remove them, add the names to \"allowShrink\" in overrides/<game>.json."
        )
    return problems

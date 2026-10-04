"""The build's reports, written to ``reports/<game>/`` beside the pack (not inside it).

- ``pending-hashes.md`` — characters in the pack with no hashes yet.
- ``left-out.md`` — what the character list has that is not in the pack, and why.
- ``missing-images.md`` — characters with no portrait, and why.
- ``inference-report.md`` — every outfit's parent, the rule that decided it, and how sure.
- ``collisions.md`` — hashes more than one character has.
- ``manual.md`` — what each file in ``manual/`` did.
- ``blocked.md`` — only when a build was refused: why, in plain words.

They are regenerated from scratch on every build, so they always describe the pack beside them.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from packbuilder.files import write_text


def plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def write(folder: pathlib.Path, game: str, assembly, variants: list[dict], hash_json: dict, missing: list[tuple[str, str]], left_out: list | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "blocked.md").unlink(missing_ok=True)
    by_name = {v["internalName"]: v for v in variants}

    pending = [v for v in variants if v.get("hashesPending")]
    lines = [
        f"# {game}: characters waiting for hashes",
        "",
        f"{plural(len(pending), 'character')} in the pack have no hashes yet. They are still usable: they appear in the "
        "grid, take mods filed by hand or by name, and offer *Learn hashes from a mod*. A shrinking list is the "
        "pipeline working; a growing one means an upstream hash repository has gone quiet.",
        "",
        "To add hashes yourself, put a file in `manual/" + game + "/hashes/` named after the character.",
        "",
    ]
    lines += [f"- **{v['internalName']}** — {v['displayName']}" + (f" (outfit of {v['baseCharacterId']})" if v.get("baseCharacterId") else "") for v in pending]
    write_text(folder / "pending-hashes.md", "\n".join(lines) + "\n")

    left_out = sorted(left_out or [], key=lambda item: (item.reason, item.key))
    lines = [
        f"# {game}: what the character list has that the pack does not",
        "",
        f"{plural(len(left_out), 'entry', 'entries')} in the character list {'is' if len(left_out) == 1 else 'are'} not in the pack. Nothing the "
        "list has is left out without being named here. To add one anyway, give it a name in \"join\" in "
        f"`overrides/{game}.json`, keyed by the entry below.",
        "",
    ]
    lines += [f"- `{item.key}` **{item.name}** — {item.reason}." for item in left_out]
    write_text(folder / "left-out.md", "\n".join(lines) + "\n")

    lines = [
        f"# {game}: characters with no portrait",
        "",
        f"{plural(len(missing), 'character')} have no picture. The app shows their initials instead. To add one, put a "
        f"picture in `manual/{game}/images/` named after the character, like `{missing[0][0] if missing else 'Name'}.png`.",
        "",
    ]
    lines += [f"- **{name}** — {by_name.get(name, {}).get('displayName', name)}: {reason}" for name, reason in sorted(missing, key=lambda m: m[0].lower())]
    write_text(folder / "missing-images.md", "\n".join(lines) + "\n")

    lines = [
        f"# {game}: which character each outfit belongs to",
        "",
        "| Outfit or folder | Belongs to | How it was decided | Confidence |",
        "|---|---|---|---|",
    ]
    lines += [f"| {i.name} | {i.parent or '— (a character of its own)'} | {i.rule} | {i.confidence} |" for i in assembly.inferences]
    write_text(folder / "inference-report.md", "\n".join(lines) + "\n")

    sharing: dict[str, set[str]] = {}
    ignored = set(hash_json.get("ignoredHashes", []))
    for entry in hash_json.get("entries", []):
        if entry["hash"] not in ignored:
            sharing.setdefault(entry["hash"], set()).add(entry["variant"])
    shared = sorted(((h, sorted(v, key=str.lower)) for h, v in sharing.items() if len(v) > 1), key=lambda item: (-len(item[1]), item[0]))
    lines = [
        f"# {game}: hashes more than one character has",
        "",
        f"{plural(len(shared), 'hash', 'hashes')} are shared (the ignored shader hashes are left out). Most are an "
        "outfit sharing its base character's parts, which is expected.",
        "",
        "| Hash | Characters |",
        "|---|---|",
    ]
    lines += [f"| `{h}` | {len(v)}: {', '.join(v)} |" for h, v in shared]
    write_text(folder / "collisions.md", "\n".join(lines) + "\n")

    lines = [f"# {game}: what manual/ added", ""]
    lines += [f"- {row}" for row in assembly.manual_rows] or ["Nothing: `manual/" + game + "/` is empty."]
    write_text(folder / "manual.md", "\n".join(lines) + "\n")


def write_blocked(folder: pathlib.Path, game: str, errors: list[str]) -> None:
    """Kept short and plain: this is what the person gets sent to when a run fails."""
    folder.mkdir(parents=True, exist_ok=True)
    lines = [f"# {game}: the build was stopped", "", "Nothing was published. The last good pack is still the one people get.", ""]
    lines += [f"- {e}" for e in errors] if errors else ["- See the run's log for the reason."]
    write_text(folder / "blocked.md", "\n".join(lines) + "\n")


@dataclass
class Changes:
    """What changed in one game's pack since the last build, for the release notes and the app.

    Names are display names. A character that is new is only in ``added`` or ``outfits``: its
    hashes and portrait are part of being new, not listed again.
    """

    added: list[str] = field(default_factory=list)
    outfits: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    first_hashes: list[str] = field(default_factory=list)
    hash_changes: list[str] = field(default_factory=list)
    new_portraits: list[str] = field(default_factory=list)
    changed_portraits: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    icon: str = ""
    other: list[str] = field(default_factory=list)
    contents: str = ""
    first: bool = False

    def any(self) -> bool:
        return self.first or any(
            (self.added, self.outfits, self.removed, self.renamed, self.first_hashes, self.hash_changes,
             self.new_portraits, self.changed_portraits, self.details, self.icon, self.other)
        )

    def lines(self, limit: int = 40) -> list[str]:
        """Full sentences, one per kind of change: the release notes and the build's log."""
        if self.first:
            return [f"First build: {self.contents}."]
        rows = [
            ("Added", self.added),
            ("New skins", self.outfits),
            ("Removed", self.removed),
            ("Renamed", self.renamed),
            ("Hashes for the first time", self.first_hashes),
            ("Hashes changed", self.hash_changes),
            ("New portraits", self.new_portraits),
            ("Changed portraits", self.changed_portraits),
            ("Other details changed", self.details),
        ]
        lines = [f"{label}: {_list(names, limit)}." for label, names in rows if names]
        if self.icon:
            lines.append(self.icon)
        return lines + self.other


def card_line(variants: list[dict]) -> str:
    """The app's pack card: what the pack holds, and nothing about what changed (the user's words,
    2026-09-27: "that's for changelog"). In the app's own words, so a card for a pack not installed
    yet reads like one the app counted itself: "124 characters and 28 skins, 19 still waiting for hashes."
    """
    skins = sum(1 for v in variants if v.get("baseCharacterId"))
    pending = sum(1 for v in variants if v.get("hashesPending"))
    text = plural(len(variants) - skins, "character") + (f" and {plural(skins, 'skin')}" if skins else "")
    return text + (f", {pending} still waiting for hashes" if pending else "") + "."


def contents(variants: list[dict]) -> str:
    """What the pack holds, for the release notes, in the app's words: characters and skins."""
    skins = sum(1 for v in variants if v.get("baseCharacterId"))
    pending = sum(1 for v in variants if v.get("hashesPending"))
    text = plural(len(variants) - skins, "character") + (f" and {plural(skins, 'skin')}" if skins else "")
    return text + (f", {pending} still waiting for hashes" if pending else "")


# Fields a character's "other details" are judged by; the name, picture and hashes have their own lines.
DETAIL_FIELDS = ("baseCharacterId", "isDefaultVariant", "modFilesName", "aliases", "releaseDate", "attributes")


def compare(
    previous_variants: list[dict],
    previous_hashes: dict,
    previous_images: dict[str, str],
    previous_game: dict,
    variants: list[dict],
    hash_json: dict,
    images: dict[str, str],
    game: dict,
) -> Changes:
    """What changed from the last pack to this one. ``*images`` map a file in ``images/`` to its checksum."""
    changes = Changes(contents=contents(variants))
    if not previous_variants:
        changes.first = True
        return changes

    def key(variant: dict) -> str:
        return variant["internalName"].lower()

    def hash_sets(payload: dict) -> dict[str, set[str]]:
        sets: dict[str, set[str]] = {}
        for entry in payload.get("entries", []):
            sets.setdefault(str(entry.get("variant", "")).lower(), set()).add(str(entry.get("hash", "")).lower())
        return sets

    def picture(variant: dict, record: dict[str, str]) -> str | None:
        image = variant.get("image")
        return record.get(image.split("/", 1)[-1]) if image else None

    before = {key(v): v for v in previous_variants}
    after = {key(v): v for v in variants}
    names = {key(v): v["displayName"] for v in variants}
    had, has = hash_sets(previous_hashes), hash_sets(hash_json)

    for name, variant in after.items():
        old = before.get(name)
        if old is None:
            if variant.get("baseCharacterId"):
                parent = after.get(variant["baseCharacterId"].lower(), {}).get("displayName", variant["baseCharacterId"])
                changes.outfits.append(f"{variant['displayName']} ({parent})")
            else:
                changes.added.append(variant["displayName"])
            continue
        if old["displayName"] != variant["displayName"]:
            changes.renamed.append(f"{old['displayName']} → {variant['displayName']}")
        old_hashes, new_hashes = had.get(name, set()), has.get(name, set())
        if new_hashes and not old_hashes:
            changes.first_hashes.append(names[name])
        elif old_hashes != new_hashes and old_hashes:
            gained, lost = len(new_hashes - old_hashes), len(old_hashes - new_hashes)
            counts = ", ".join(c for c in (f"+{gained}" if gained else "", f"−{lost}" if lost else "") if c)
            changes.hash_changes.append(f"{names[name]} ({counts})")
        old_picture, new_picture = picture(old, previous_images), picture(variant, images)
        if new_picture and not old_picture:
            changes.new_portraits.append(names[name])
        elif new_picture and new_picture != old_picture:
            changes.changed_portraits.append(names[name])
        if any(old.get(f) != variant.get(f) for f in DETAIL_FIELDS):
            changes.details.append(names[name])
    changes.removed = [v["displayName"] for n, v in before.items() if n not in after]

    icon_before, icon_after = previous_images.get("_game.webp"), images.get("_game.webp")
    if icon_after and icon_after != icon_before:
        changes.icon = "New game icon." if icon_before else "Added the game's icon."
    if {k: v for k, v in previous_game.items() if k != "icon"} != {k: v for k, v in game.items() if k != "icon"}:
        changes.other.append("The game's details changed (its name, filters or folder settings).")
    if sorted(previous_hashes.get("ignoredHashes", [])) != sorted(hash_json.get("ignoredHashes", [])):
        changes.other.append("The list of shared shader hashes changed.")
    return changes


def _list(names: list[str], limit: int = 12) -> str:
    names = sorted(names, key=str.lower)
    if len(names) > limit:
        return ", ".join(names[:limit]) + f" and {len(names) - limit} more"
    return ", ".join(names)

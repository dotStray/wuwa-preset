"""Joining the roster stage and the hash stage into one list of variants.

The two stages meet here and nowhere else. The rules, in order:

1. **A roster character keeps the name it was first published under.** ``ledger/<game>.json``
   remembers every roster entry's internal name, because an internal name is a Mods folder on
   someone's disk and is stable forever (``PRESET_SCHEMA.md`` §1).
2. **A roster character is joined to a hash entry by name**: an override first, then its
   own internal name, then its whole name, and only after every character has had that
   chance, by one word of a longer name ("Luuk" for "Luuk Herssen").
3. **A roster character with no hash entry is still a character**, with ``hashesPending``. A
   second roster entry with the same name as a character already in the pack, and nothing in the
   ledger or the overrides to say who it is, is not added twice: it is reported as left out.
4. **An outfit the character list names is an outfit from that day**, joined to the hash entry
   named after its character and its name ("CarlottaSplashingSummer" for Splashing Summer), now
   or whenever the entry arrives. It keeps the name it was first published under.
5. **A hash entry no roster character or outfit claimed never becomes a character**: only the
   character list decides who is in. ``overrides/<game>.json`` says what it is (``partOf`` a
   character or outfit, ``parents`` for an outfit, ``exclude`` to leave it out). Without one, an
   entry whose name starts with a character's or outfit's is a part of that model (Augusta's
   summoned sword, Zani's upper body): its hashes are that variant's. One that starts with a
   character's name while that character has outfits still waiting for hashes might be one of
   them, and stops the build until the overrides say. One that names no one is left out, named in
   the report and in the run's notes until the overrides say what it is.
6. **``manual/`` is added last.** Hand-added hashes are kept beside upstream's, duplicates
   removed (the user's ruling, 2026-09-25); a file in ``hashes/replace/`` stands in for
   upstream's hashes of its character entirely (their ruling, 2026-10-02).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packbuilder.hashes import Folder, entries as folder_entries
from packbuilder.manual import Manual
from packbuilder.names import ascii_words, is_valid_id, join_key, pascal, split_camel
from packbuilder.roster import Character, LeftOut
from packbuilder.settings import Overrides

HIGH, MEDIUM = "high", "medium"

# Words no hash folder is named after.
FILLER = {"the", "and", "for", "with", "from"}


@dataclass
class Variant:
    name: str
    display: str
    parent: str | None = None
    attributes: dict = field(default_factory=dict)
    image_url: str | None = None
    image_crop: str | None = None  # how to cut the source's picture, when not as config says
    image_path: object = None  # a manual picture's path
    roster_key: str | None = None
    folder: Folder | None = None
    hashes: list[dict] = field(default_factory=list)
    origin: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass
class Inference:
    name: str
    parent: str | None
    rule: str
    confidence: str
    note: str = ""


@dataclass
class Assembly:
    variants: list[Variant]
    ignored: list[str]
    attributes: dict
    inferences: list[Inference]
    ledger: dict[str, str]
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    manual_rows: list[str] = field(default_factory=list)
    left_out: list[LeftOut] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)  # variants whose hashes manual/…/hashes/replace/ gave


class _Names:
    """Every internal name in use, compared ignoring case."""

    def __init__(self) -> None:
        self.by_key: dict[str, Variant] = {}

    def get(self, name: str | None) -> Variant | None:
        return self.by_key.get(name.lower()) if name else None

    def add(self, variant: Variant) -> Variant:
        self.by_key[variant.name.lower()] = variant
        return variant

    def unique(self, wanted: str, reserved: set[str]) -> str:
        wanted = wanted or "Unnamed"
        candidate, number = wanted, 2
        while candidate.lower() in self.by_key or candidate.lower() in reserved:
            candidate, number = f"{wanted}{number}", number + 1
        return candidate


def assemble(config: dict, overrides: Overrides, roster: list[Character], folders: list[Folder], ledger: dict[str, str], manual: Manual) -> Assembly:
    errors: list[str] = []
    notes: list[str] = []
    names = _Names()
    ledger = dict(ledger)

    folders_by_key: dict[str, Folder] = {}
    for folder in folders:
        if folder.name.lower() in folders_by_key:
            errors.append(
                f"Two upstream folders are both called '{folder.name}' ignoring capitals "
                f"({folders_by_key[folder.name.lower()].path}, {folder.path}). Internal names ignore case, so one "
                "has to go: add one to \"parents\" in overrides as a rename is not possible, or ask upstream."
            )
            continue
        folders_by_key[folder.name.lower()] = folder
    claimed: dict[str, str] = {}  # folder key → variant name

    def free(key: str | None) -> Folder | None:
        folder = folders_by_key.get(key.lower()) if key else None
        return folder if folder and folder.name.lower() not in claimed else None

    excluded = set(overrides.exclude)
    characters = [c for c in roster if c.key not in excluded]
    left_out: list[LeftOut] = []

    def family_name(family: str | None) -> str | None:
        """The name another form of the same character goes by, so every form is one character."""
        if not family:
            return None
        for table in (overrides.join, ledger):
            for key, value in table.items():
                if key == family or key.startswith(family + "-"):
                    return value
        return None

    # ---- 1. Roster characters: decide each one's name and folder. ----------------------------
    chosen: dict[str, tuple[str | None, Folder | None]] = {}
    for character in characters:
        name = overrides.join.get(character.key) or ledger.get(character.key) or family_name(character.family)
        folder = free(name)
        if folder:
            claimed[folder.name.lower()] = name or folder.name
        chosen[character.key] = (name, folder)

    for keys in ("join_keys", "word_keys"):
        for character in characters:
            name, folder = chosen[character.key]
            if folder or character.key in overrides.join:
                continue
            for key in getattr(character, keys):
                candidate = free(key)
                if candidate:
                    claimed[candidate.name.lower()] = candidate.name
                    chosen[character.key] = (name, candidate)
                    break

    attribute_values = _AttributeMapper(config.get("attributes", {}), notes)
    by_name: dict[str, Variant] = {}
    source_names: dict[str, str] = {}  # variant → the name the character list gave it
    for character in characters:
        name, folder = chosen[character.key]
        name = name or (folder.name if folder else None)
        existing = names.get(name)
        if existing:
            # Several roster entries for one character (the Traveler, once per element).
            existing.attributes = attribute_values.merge(existing.attributes, attribute_values.map(character.attributes))
            ledger[character.key] = existing.name
            continue
        if not name:
            twin = names.get(pascal(character.name))
            if twin is not None and twin.origin == "roster" and join_key(source_names.get(twin.name, "")) == join_key(character.name):
                # Gachabase lists some characters twice: an unfinished copy, an event's version. A
                # second "Remielle" is not a new character called Remielle2.
                outfits = f" (and its {len(character.outfits)} outfit{'s' if len(character.outfits) != 1 else ''})" if character.outfits else ""
                left_out.append(
                    LeftOut(
                        character.key,
                        character.name + outfits,
                        f"another entry, {twin.roster_key}, has the same name and is in the pack as {twin.name}. "
                        f"If this one is a character of its own, give it a name in \"join\" in overrides",
                    )
                )
                continue
            name = names.unique(pascal(character.name), set(folders_by_key) - set(claimed))
        if not is_valid_id(name):
            errors.append(f"Roster entry {character.key} ('{character.name}') would be called '{name}', which is not a valid id. Add it to \"join\" in overrides.")
            continue
        ledger[character.key] = name
        variant = names.add(
            Variant(
                name=name,
                display=overrides.display_names.get(name, character.name),
                attributes=attribute_values.map(character.attributes),
                image_url=character.image,
                roster_key=character.key,
                folder=folder,
                origin="roster",
            )
        )
        if folder:
            claimed[folder.name.lower()] = name
        by_name[character.key] = variant
        source_names[name] = character.name

    # ---- 2. Outfits the character list names. -------------------------------------------------
    pending_outfits: dict[str, list[str]] = {}
    wanted: list[tuple[Variant, object]] = []  # (base character, outfit), each outfit once
    seen_outfits: set[str] = set()
    named_outfits: dict[tuple[str, str], str] = {}  # (base character, outfit name) → the outfit's key
    for character in characters:
        base = by_name.get(character.key) or names.get(ledger.get(character.key))
        if base is None:
            continue  # left out above, with its outfits
        for outfit in character.outfits:
            if outfit.key in seen_outfits or outfit.key in excluded:
                continue
            seen_outfits.add(outfit.key)
            known = overrides.join.get(outfit.key) or ledger.get(outfit.key)
            if not outfit.name and not known:
                left_out.append(LeftOut(outfit.key, f"an outfit of {base.display}", "it has no name in the source yet; it is added the week it gets one"))
                continue
            if outfit.name:
                # A character the list has once per attribute (Rover) has its outfit listed under each entry.
                twin = named_outfits.setdefault((base.name.lower(), join_key(outfit.name)), outfit.key)
                if twin != outfit.key and not known:
                    left_out.append(LeftOut(outfit.key, f"{outfit.name} ({base.display})", f"the same outfit as {twin}, listed again under another of {base.display}'s entries"))
                    continue
            wanted.append((base, outfit))

    # A published outfit claims its own folder first, so a new outfit's words never take it.
    chosen_outfits: dict[str, tuple[str | None, Folder | None]] = {}
    for base, outfit in wanted:
        name = overrides.join.get(outfit.key) or ledger.get(outfit.key)
        folder = free(name)
        if folder:
            claimed[folder.name.lower()] = name
        chosen_outfits[outfit.key] = (name, folder)
    for base, outfit in wanted:
        name, folder = chosen_outfits[outfit.key]
        if folder or outfit.key in overrides.join or not outfit.name:
            continue
        # The hash source names an outfit after its character and its name, or a word of it
        # ("CarlottaSplashingSummer" for Splashing Summer), so that entry is this outfit's, whenever
        # it arrives. One that has waited keeps its name.
        for word in [pascal(w) for w in ascii_words(outfit.name)] + [pascal(outfit.name)]:
            candidate = free(base.name + word)
            if candidate:
                claimed[candidate.name.lower()] = name or candidate.name
                chosen_outfits[outfit.key] = (name, candidate)
                break

    for base, outfit in wanted:
        name, folder = chosen_outfits[outfit.key]
        if not name and folder:
            name = folder.name
        if not name:
            # Named like the entry that will come: the character and the outfit's first word that
            # says something ("ChangliLaurel" for Laurel Nymph).
            words = [w for w in ascii_words(outfit.name) if len(w) > 2 and w.lower() not in FILLER] or ascii_words(outfit.name)
            # Not a folder's name, and not a name another outfit already goes by.
            reserved = set(folders_by_key) | {n.lower() for n, _ in chosen_outfits.values() if n}
            short = base.name + (pascal(words[0]) if words else "")
            name = short if words and not names.get(short) and short.lower() not in reserved else names.unique(base.name + pascal(outfit.name), reserved)
        if names.get(name):
            errors.append(f"Outfit {outfit.key} ('{outfit.name or name}') would be called '{name}', which another variant already is. Fix \"join\" in overrides.")
            continue
        ledger[outfit.key] = name
        if folder:
            claimed[folder.name.lower()] = name
        else:
            pending_outfits.setdefault(base.name, []).append(outfit.name or name)
        remainder = name[len(base.name):] if name.lower().startswith(base.name.lower()) else ""
        display = overrides.display_names.get(name) or outfit.name or (f"{base.display} {split_camel(remainder)}" if remainder else split_camel(name))
        names.add(
            Variant(
                name=name,
                display=display,
                parent=base.name,
                attributes=dict(base.attributes),
                image_url=outfit.image,
                image_crop=outfit.crop,
                roster_key=outfit.key,
                folder=folder,
                origin="roster outfit",
            )
        )

    # ---- 3. Hash entries nobody claimed. --------------------------------------------------------
    inferences: list[Inference] = []
    prefixes: dict[str, str] = {}
    for variant in list(names.by_key.values()):
        prefixes.setdefault(variant.name.lower(), variant.name)
    words: dict[str, set[str]] = {}
    for character in characters:
        target = ledger.get(character.key)
        if not target:
            continue
        for key in character.join_keys:
            if len(key) >= 4:
                prefixes.setdefault(key, target)
        for key in character.word_keys:
            if len(key) >= 4:
                words.setdefault(key, set()).add(target)
    for key, targets in words.items():
        # One word of a longer name ("luuk" for Luuk Herssen), but only a word no two characters share.
        if len(targets) == 1:
            prefixes.setdefault(key, next(iter(targets)))

    parts: dict[str, list] = {}  # variant → entries that are parts of its model
    unclaimed = [f for f in folders if f.name.lower() in folders_by_key and f.name.lower() not in claimed and folders_by_key[f.name.lower()] is f]
    for folder in sorted(unclaimed, key=lambda f: f.name.lower()):
        name = folder.name
        if name in excluded:
            left_out.append(LeftOut(f"hashes:{name}", name, "overrides \"exclude\" leaves it out"))
            continue
        if not folder_entries(name, folder.components):
            left_out.append(LeftOut(f"hashes:{name}", name, "the hash source has no hashes in it"))
            continue
        if name in overrides.part_of:
            owner = names.get(overrides.part_of[name])
            if owner is None:
                errors.append(f"overrides \"partOf\" says '{name}' is part of '{overrides.part_of[name]}', and there is no '{overrides.part_of[name]}'.")
                continue
            parts.setdefault(owner.name, []).append(folder)
            claimed[name.lower()] = owner.name
            inferences.append(Inference(name, owner.name, f"override: part of {owner.name}, its hashes are {owner.name}'s", HIGH))
            continue
        if name in overrides.parents:
            parent = overrides.parents[name]
            base = names.get(parent) if parent else None
            if base is None:
                errors.append(
                    f"overrides \"parents\" says '{name}' is an outfit of '{parent}', and there is no '{parent}'. "
                    "Only the character list makes a character; to keep these hashes, say whose they are."
                )
                continue
            if not is_valid_id(name):
                errors.append(f"overrides \"parents\" makes '{name}' an outfit, and '{name}' is not a valid id. Use \"partOf\" with an outfit the character list names.")
                continue
            remainder = name[len(base.name):] if name.lower().startswith(base.name.lower()) and len(name) > len(base.name) else ""
            display = overrides.display_names.get(name) or (f"{base.display} {split_camel(remainder)}" if remainder else split_camel(name))
            names.add(Variant(name=name, display=display, parent=base.name, attributes=dict(base.attributes), folder=folder, origin="hash entry"))
            claimed[name.lower()] = name
            inferences.append(Inference(name, base.name, "override: an outfit of " + base.name, HIGH))
            continue
        match = max((k for k in prefixes if name.lower().startswith(k) and len(k) < len(name)), key=len, default=None)
        if match is None:
            left_out.append(LeftOut(f"hashes:{name}", name, "no character or outfit on the character list has this name"))
            notes.append(
                f"The hash source has '{name}', and no character on the character list has that name. Its hashes are left out. "
                f"Say whose they are in overrides/<game>.json (\"partOf\": {{\"{name}\": \"<character>\"}}), or leave it out for good (\"exclude\")."
            )
            continue
        owner = names.get(prefixes[match])
        if owner is None:
            left_out.append(LeftOut(f"hashes:{name}", name, f"its name starts with '{name[:len(match)]}', which nobody in the pack is called any more"))
            continue
        if owner.parent is None and pending_outfits.get(owner.name):
            errors.append(
                f"Not sure what '{folder.path}' in the hash source is. It starts with {owner.name}'s name, and {owner.name} has outfits with no "
                f"hashes yet ({', '.join(pending_outfits[owner.name])}): it may be one of them. Say which in overrides/<game>.json: "
                f"\"partOf\": {{\"{name}\": \"<the outfit, or {owner.name}>\"}}."
            )
            continue
        parts.setdefault(owner.name, []).append(folder)
        claimed[name.lower()] = owner.name
        inferences.append(Inference(name, owner.name, f"name starts with '{name[:len(match)]}': a part of {owner.name}'s model, its hashes are {owner.name}'s", MEDIUM))

    for variant in names.by_key.values():
        if variant.origin == "roster outfit":
            inferences.append(Inference(variant.name, variant.parent, "the character list names it as an outfit", HIGH))

    # ---- 4. Hashes from the folders. ------------------------------------------------------------
    for variant in names.by_key.values():
        if variant.folder:
            variant.hashes = folder_entries(variant.name, variant.folder.components)
        seen = {(e["kind"], e["hash"], e.get("textureKind"), e.get("slot")) for e in variant.hashes}
        for part in parts.get(variant.name, []):
            for entry in folder_entries(variant.name, part.components):
                key = (entry["kind"], entry["hash"], entry.get("textureKind"), entry.get("slot"))
                if key not in seen:
                    seen.add(key)
                    variant.hashes.append(entry)

    # ---- 5. manual/. ------------------------------------------------------------------------------
    manual_rows: list[str] = []
    errors.extend(manual.problems)

    def resolve(written: str) -> Variant | None:
        found = names.get(written) or names.get(pascal(written))
        if found:
            return found
        wanted = join_key(written)
        matches = [v for v in names.by_key.values() if join_key(v.display) == wanted]
        return matches[0] if len(matches) == 1 else None

    for item in manual.characters:
        if resolve(item.internal_name or item.name):
            manual_rows.append(f"`{item.source}`: '{item.name}' is already in the pack; nothing to add.")
            continue
        parent = None
        if item.outfit_of:
            parent_variant = resolve(item.outfit_of)
            if not parent_variant:
                errors.append(f"{item.source}: '{item.name}' is an outfit of '{item.outfit_of}', and there is no such character.")
                continue
            parent = _root(names, parent_variant.name)
        name = item.internal_name or names.unique(pascal(item.name), set())
        if not is_valid_id(name):
            errors.append(f"{item.source}: '{name}' is not a valid id (letters, digits, - and _ only).")
            continue
        base = names.get(parent)
        names.add(Variant(name=name, display=item.display_name or item.name, parent=parent, attributes=dict(base.attributes) if base else {}, origin="manual"))
        manual_rows.append(f"`{item.source}`: added **{name}**" + (f", an outfit of {parent}." if parent else "."))

    replaced: list[str] = []
    for written, found in sorted(manual.replacements.items()):
        # Stands in for upstream entirely, for as long as the file is there (the user's ruling, 2026-10-02).
        source = manual.replacement_sources.get(written, "manual")
        variant = resolve(written)
        if variant is None:
            errors.append(
                f"{source}: there is no character called '{written}', so there is nothing for it to replace. "
                "Rename the file to the character's name, or move it up into hashes/ to add a new character."
            )
            continue
        before = len(variant.hashes)
        variant.hashes = []
        have: set = set()
        for entry in found:
            entry = {**entry, "variant": variant.name}
            key = (entry["kind"], entry["hash"], entry.get("textureKind"), entry.get("slot"))
            if key not in have:
                have.add(key)
                variant.hashes.append(entry)
        replaced.append(variant.name)
        manual_rows.append(
            f"`{source}` → **{variant.name}**: replaces upstream's {before} hash{'es' if before != 1 else ''} "
            f"with the file's {len(variant.hashes)}. Upstream's are used again once the file is deleted."
        )

    for written, found in sorted(manual.hashes.items()):
        source = manual.hash_sources.get(written, "manual")
        variant = resolve(written)
        if variant is None:
            name = pascal(written)
            if not is_valid_id(name):
                errors.append(f"{source}: '{written}' cannot be turned into a character id.")
                continue
            variant = names.add(Variant(name=name, display=written if " " in written else split_camel(written), origin="manual"))
            manual_rows.append(f"`{source}`: no character called '{written}', so **{name}** was added as a new one. If that was a typo, rename the file.")
        upstream_values = {e["hash"] for e in variant.hashes}
        have = {(e["kind"], e["hash"], e.get("textureKind"), e.get("slot")) for e in variant.hashes}
        added = redundant = 0
        for entry in found:
            entry = {**entry, "variant": variant.name}
            key = (entry["kind"], entry["hash"], entry.get("textureKind"), entry.get("slot"))
            if entry["hash"] in upstream_values and variant.folder is not None:
                redundant += 1
            if key in have or (entry["kind"] == "unknown" and entry["hash"] in upstream_values):
                continue
            have.add(key)
            variant.hashes.append(entry)
            added += 1
        row = f"`{source}` → **{variant.name}**: {added} hash{'es' if added != 1 else ''} added"
        if redundant:
            row += f"; {redundant} of the file's hashes are now in upstream too (safe to delete the file once all are)"
        manual_rows.append(row + ".")

    for written, path in sorted(manual.images.items()):
        variant = resolve(written)
        if variant is None:
            errors.append(f"manual picture '{path.name}': there is no character called '{written}'. Rename the file to the character's name.")
            continue
        if written not in manual.kept_images and variant.image_url:
            # The source has caught up: its picture takes over by itself, and nobody is reminded (their choice).
            manual_rows.append(
                f"`manual/…/images/{path.name}` is not used any more: the source has a picture of **{variant.name}** now. "
                "Safe to delete; to keep using yours instead, move it into `images/keep/`."
            )
            continue
        variant.image_path = path
        kept = " (from `images/keep/`: used whatever the source has)" if written in manual.kept_images else ""
        manual_rows.append(f"`manual/…/images/{path.name}` is **{variant.name}**'s portrait{kept}.")

    for name, alias_list in overrides.aliases.items():
        variant = names.get(name)
        if variant is None:
            errors.append(f"overrides \"aliases\" names '{name}', and there is no such character.")
        else:
            variant.aliases = list(alias_list)

    for key, name in overrides.display_names.items():
        if not names.get(name) and not names.get(key):
            errors.append(f"overrides \"displayNames\" names '{key}', and there is no such character.")
    for key in overrides.join:
        if key not in {c.key for c in roster} and not any(o.key == key for c in roster for o in c.outfits):
            notes.append(f"overrides \"join\" names roster entry '{key}', which the character list no longer has.")
    for name in [*overrides.parents, *overrides.part_of]:
        if name.lower() not in folders_by_key:
            notes.append(f"overrides names '{name}', and the hash source has no such entry (any more).")

    variants = sorted(names.by_key.values(), key=lambda v: v.name.lower())
    ignored = sorted({e["hash"] for v in variants for e in v.hashes if e["kind"] == "root_vs"} | set(overrides.ignored_hashes))
    return Assembly(
        variants=variants,
        ignored=ignored,
        attributes=attribute_values.declared(),
        inferences=sorted(inferences, key=lambda i: i.name.lower()),
        ledger=dict(sorted(ledger.items())),
        errors=errors,
        notes=notes,
        manual_rows=manual_rows,
        left_out=left_out,
        replaced=replaced,
    )


def _root(names: _Names, name: str) -> str:
    """A family has one level: an outfit of an outfit belongs to the base character."""
    seen = set()
    variant = names.get(name)
    while variant and variant.parent and variant.name.lower() not in seen:
        seen.add(variant.name.lower())
        variant = names.get(variant.parent)
    return variant.name if variant else name


class _AttributeMapper:
    """Turns a source's raw values ("Ice", "WEAPON_BOW") into the pack's ids ("cryo", "bow").

    The table is ``config/<game>.json``. A value the table has never seen is not dropped: it
    becomes an id of its own and a note in the build report, so a new element or weapon shows
    up as a filter the week it appears instead of vanishing.
    """

    def __init__(self, table: dict, notes: list[str]):
        self.table = table
        self.notes = notes
        self.extra: dict[str, dict[str, str]] = {}
        self.lookup: dict[str, dict[str, str | None]] = {}
        for attribute, spec in table.items():
            mapping: dict[str, str | None] = {}
            for value in spec.get("values", []):
                for raw in value.get("from", []):
                    mapping[str(raw).lower()] = value["id"]
            for raw in spec.get("ignore", []):
                mapping[str(raw).lower()] = None
            self.lookup[attribute] = mapping

    def map(self, raw: dict) -> dict:
        result: dict = {}
        for attribute, value in raw.items():
            spec = self.table.get(attribute)
            if spec is None:
                continue
            if spec.get("kind") == "number":
                if isinstance(value, (int, float)):
                    result[attribute] = value
                continue
            values = value if isinstance(value, list) else [value]
            mapped = [m for m in (self._one(attribute, v) for v in values) if m]
            mapped = list(dict.fromkeys(mapped))
            if not mapped:
                continue
            result[attribute] = mapped if spec.get("list") else mapped[0]
        return result

    def _one(self, attribute: str, raw: object) -> str | None:
        key = str(raw).lower()
        mapping = self.lookup[attribute]
        if key in mapping:
            return mapping[key]
        new_id = join_key(str(raw)) or None
        if new_id and new_id not in self.extra.setdefault(attribute, {}):
            self.extra[attribute][new_id] = split_camel(str(raw)).title()
            self.notes.append(
                f"New {attribute} value '{raw}' from the character list; published as '{new_id}'. "
                f"Give it a proper name in config (\"attributes\" → \"{attribute}\")."
            )
        mapping[key] = new_id
        return new_id

    def merge(self, left: dict, right: dict) -> dict:
        merged = dict(left)
        for attribute, value in right.items():
            if attribute not in merged:
                merged[attribute] = value
                continue
            if self.table.get(attribute, {}).get("kind") == "number":
                continue
            current = merged[attribute] if isinstance(merged[attribute], list) else [merged[attribute]]
            extra = value if isinstance(value, list) else [value]
            combined = list(dict.fromkeys(current + extra))
            merged[attribute] = combined if len(combined) > 1 or self.table.get(attribute, {}).get("list") else combined[0]
        return merged

    def declared(self) -> dict:
        declared = {}
        for attribute, spec in self.table.items():
            entry = {"displayName": spec["displayName"]}
            if spec.get("kind"):
                entry["kind"] = spec["kind"]
            if spec.get("kind") != "number":
                values = [{"id": v["id"], "displayName": v["displayName"]} for v in spec.get("values", [])]
                known = {v["id"] for v in values}
                values += [{"id": i, "displayName": d} for i, d in sorted(self.extra.get(attribute, {}).items()) if i not in known]
                entry["values"] = values
            declared[attribute] = entry
        return declared

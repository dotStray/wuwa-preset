"""One game's build, start to finish.

Stages, each of which can fail on its own without taking the others down:

1. **Roster** — refresh ``upstream/<game>/roster.json`` from the character list.
2. **Hashes** — refresh ``upstream/<game>/hashes/`` from the hash source.
3. **Assemble** — join them, add ``manual/``, apply ``overrides/``.
4. **Portraits** — fetch and shrink any picture not already in the pack.
5. **Check** — validate, then compare with the published pack (:mod:`packbuilder.checks`).
6. **Write** — only now does ``packs/<game>/`` change, all at once.

If a source cannot be reached, its stage keeps the copy from the last run and says so;
the rest of the build carries on. If a check fails, nothing under ``packs/`` or ``ledger/``
is touched and the reasons are returned.
"""

from __future__ import annotations

import datetime
import hashlib
import pathlib
import re
import shutil
import traceback
from dataclasses import dataclass, field

from packbuilder import BUILDER, assemble as assembler, checks, hashes, images, manual, reports, roster
from packbuilder.files import BuildError, Repo, dumps, read_json, swap_in, write_bytes, write_json, write_text
from packbuilder.http import Fetcher, FetchError
from packbuilder.settings import load_config, load_overrides

MIN_APP_VERSION = "0.1.0"
IMAGE_LICENCE = "Game art © Kuro Games, as served by the source named in each image's record."


@dataclass
class GameResult:
    game: str
    changed: bool = False
    version: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    changes: reports.Changes | None = None


def build_game(
    repo: Repo,
    game: str,
    *,
    fetcher: Fetcher | None,
    refresh_roster: bool = True,
    refresh_hashes: bool = True,
    today: datetime.date | None = None,
    known_versions: set[str] | None = None,
) -> GameResult:
    result = GameResult(game)
    try:
        return _build(repo, game, fetcher, refresh_roster, refresh_hashes, today or datetime.datetime.now(datetime.timezone.utc).date(), known_versions or set(), result)
    except BuildError as error:
        result.errors.append(str(error))
    except Exception as error:  # noqa: BLE001 — one game's surprise must not stop the other two (audit P3)
        result.errors.append(
            f"The build of {game} stopped on something it did not expect ({type(error).__name__}: {error}).\n\n"
            f"```\n{traceback.format_exc().rstrip()}\n```"
        )
    # Whatever stopped it: the reason is in reports/<game>/blocked.md, and no half-built pack is left (P8).
    reports.write_blocked(repo.reports(game), game, result.errors)
    staging = repo.pack(game).with_name(f".{game}.new")
    if staging.exists():
        shutil.rmtree(staging)
    return result


def _build(repo, game, fetcher, refresh_roster, refresh_hashes, today, known_versions, result: GameResult) -> GameResult:
    config = load_config(repo.config(game))
    overrides = load_overrides(repo.overrides(game))
    upstream = repo.upstream(game)
    pack_dir = repo.pack(game)

    previous_manifest = read_json(pack_dir / "manifest.json", {}) or {}
    previous_variants = read_json(pack_dir / "variants.json", []) or []
    previous_hashes = read_json(pack_dir / "hashes.json", {}) or {}
    previous_game = read_json(pack_dir / "game.json", {}) or {}
    previous_images = image_checksums(pack_dir / "images")
    previous_fingerprint = fingerprint(pack_dir)

    # 1. Roster.
    roster_path = upstream / "roster.json"
    saved = read_json(roster_path, {}) or {}
    characters = [roster.Character.from_json(c) for c in saved.get("characters", [])]
    left_out = [roster.LeftOut.from_json(c) for c in saved.get("leftOut", [])]
    if saved.get("source") and saved.get("source") != config["roster"]["source"]:
        # A list from another source holds other kinds of keys and none of this source's checksums.
        # It is still the fallback when the new source cannot be read, and gives each its last name.
        result.warnings.append(f"The character list's source is now {config['roster']['source']} (was {saved.get('source') or 'none'}).")
    if fetcher is not None and refresh_roster:
        try:
            fresh = roster.read(fetcher, characters, config["roster"])
            if characters and len(fresh.characters) < len(characters) * 0.9:
                raise FetchError(
                    f"the character list shrank from {len(characters)} to {len(fresh.characters)} entries, which looks like a broken answer"
                )
            characters, left_out = fresh.characters, fresh.left_out
            result.warnings.extend(fresh.warnings)
            write_json(
                roster_path,
                {
                    "source": config["roster"]["source"],
                    "characters": [c.to_json() for c in characters],
                    **({"leftOut": [c.to_json() for c in left_out]} if left_out else {}),
                },
            )
        except FetchError as error:
            result.warnings.append(f"Character list not refreshed ({error}); used the copy from the last run.")
    if not characters:
        raise BuildError(f"There is no character list for {game} yet, and it could not be fetched.")

    # 2. Hashes.
    hash_config = config["hashes"]
    hash_dir = upstream / "hashes"
    if fetcher is not None and refresh_hashes:
        try:
            hashes.refresh(hash_config["repo"], hash_config["path"], hash_dir, fetcher)
        except FetchError as error:
            result.warnings.append(f"Hashes not refreshed ({error}); used the copy from the last run.")
    folders, lock = hashes.load(hash_dir)
    if not folders:
        raise BuildError(f"There is no copy of {hash_config['repo']}'s {hash_config['path']} for {game} yet, and it could not be fetched.")
    for folder in folders:
        for value in folder.not_hashes:
            result.warnings.append(f"'{folder.path}' in the hash source gives '{value}' as a hash, and a hash is 8 or 16 hex digits: left out.")

    # 3. Assemble.
    ledger = (read_json(repo.ledger(game), {}) or {}).get("names", {})
    hand = manual.read(repo.manual(game))
    assembly = assembler.assemble(config, overrides, characters, folders, ledger, hand)
    result.warnings.extend(assembly.notes)
    if assembly.errors:
        result.errors.extend(assembly.errors)
        reports.write_blocked(repo.reports(game), game, assembly.errors)
        return result

    # 4. Portraits, into a staging copy of the pack.
    staging = pack_dir.with_name(f".{game}.new")
    if staging.exists():
        shutil.rmtree(staging)
    if (pack_dir / "images").is_dir():
        shutil.copytree(pack_dir / "images", staging / "images", symlinks=True)
    else:
        (staging / "images").mkdir(parents=True)
    record = read_json(upstream / "images.json", {}) or {}
    portraits = config.get("portraits", {})
    pictures = images.build(assembly.variants, staging / "images", record, portraits.get("crop", "none"), fetcher)
    icon_record = read_json(upstream / "icon.json", {}) or {}
    icon_problems, icon_record = game_icon(
        config, hand, fetcher, pack_dir / "images", staging / "images", icon_record, assembly.manual_rows, result.warnings
    )
    if icon_problems:
        shutil.rmtree(staging)
        result.errors.extend(icon_problems)
        reports.write_blocked(repo.reports(game), game, icon_problems)
        return result

    # 5. Pack files, checked before anything is replaced.
    game_json = {
        "gameId": game,
        "displayName": config["displayName"],
        **({"shortName": config["shortName"]} if config.get("shortName") else {}),
        **({"importer": config["importer"]} if config.get("importer") else {}),
        **({"icon": "images/_game.webp"} if (staging / "images" / "_game.webp").is_file() else {}),
        "disabledPrefix": config.get("disabledPrefix", "DISABLED_"),
        "attributes": assembly.attributes,
    }
    variants = [variant_json(v, pictures.images.get(v.name)) for v in assembly.variants]
    hash_json = {"ignoredHashes": assembly.ignored, "entries": [e for v in assembly.variants for e in v.hashes]}
    sizes = {f"images/{p.name}": p.stat().st_size for p in (staging / "images").glob("*") if p.is_file()}

    problems = checks.validate(game_json, variants, hash_json, sizes)
    # A replacement in manual/ is a person's deliberate choice, so it may be smaller than upstream's.
    problems += checks.guard(previous_variants, previous_hashes, variants, hash_json, overrides.retired, overrides.allow_shrink + assembly.replaced)
    if problems:
        shutil.rmtree(staging)
        result.errors.extend(problems)
        reports.write_blocked(repo.reports(game), game, problems)
        return result

    write_text(staging / "game.json", dumps(game_json))
    write_text(staging / "variants.json", dumps(variants))
    write_text(staging / "hashes.json", dumps(hash_json))
    write_text(staging / "ATTRIBUTION.md", attribution(config, pictures.sources, icon_source=icon_record.get("source") if "icon" in game_json else None))

    # 6. Version, manifest, and the switch.
    changed = fingerprint(staging) != previous_fingerprint or not previous_manifest
    changes = reports.compare(
        previous_variants, previous_hashes, previous_images, previous_game,
        variants, hash_json, image_checksums(staging / "images"), game_json,
    )
    if changed and not changes.any():
        changes.other.append("Small changes to the pack's files.")
    if changed:
        version = next_version(today, known_versions | {previous_manifest.get("packVersion", "")})
        skins = sum(1 for v in variants if v.get("baseCharacterId"))
        manifest = {
            "packSchemaVersion": 1,
            "gameId": game,
            "packVersion": version,
            "generatedAt": f"{today.isoformat()}T00:00:00Z",
            "builder": BUILDER,
            "authoredBy": "official",
            "minAppVersion": MIN_APP_VERSION,
            "counts": {"variants": len(variants), "skins": skins, "images": len(pictures.images)},
            "sources": sources(config, lock),
        }
    else:
        manifest = previous_manifest
    write_text(staging / "manifest.json", dumps(manifest))

    swap_in(staging, pack_dir)
    write_json(upstream / "images.json", pictures.sources)
    if icon_record:
        write_json(upstream / "icon.json", icon_record)
    write_json(repo.ledger(game), {"names": assembly.ledger})
    reports.write(repo.reports(game), game, assembly, variants, hash_json, pictures.missing, left_out + assembly.left_out)

    result.changed = changed
    result.version = manifest["packVersion"]
    result.changes = changes if changed else reports.Changes(contents=changes.contents)
    result.summary = result.changes.lines()
    return result


def game_icon(
    config: dict,
    hand: manual.Manual,
    fetcher: Fetcher | None,
    previous_images: pathlib.Path,
    pack_images: pathlib.Path,
    record: dict,
    rows: list[str],
    warnings: list[str],
) -> tuple[list[str], dict]:
    """Writes ``images/_game.webp``, and returns what stops the build and the icon's record.

    ``manual/<game>/images/_game.*`` wins. Otherwise the game's current app icon from Google Play
    (``config`` → ``icon`` → ``googlePlay``), downloaded again only when its address changes, which
    is when the game changes it — an anniversary badge comes and goes by itself. A store that cannot
    be reached keeps last week's icon, like every other source.
    """
    target = pack_images / "_game.webp"
    previous = previous_images / "_game.webp"

    if hand.game_icon is not None:
        source = f"manual/{hand.game_icon.parent.parent.name}/images/{hand.game_icon.name}"
        if hand.game_icon.is_symlink():
            return [f"{source} is a link, and a link is not read. Put the picture itself there."], record
        try:
            data = images.game_icon(hand.game_icon.read_bytes())
        except (OSError, ValueError) as error:
            return [f"{source} is not a picture this builder can read ({error}). Replace it with a .png, .jpg or .webp."], record
        write_bytes(target, data)
        rows.append(f"`{source}` is the game's icon.")
        return [], {**record, "source": source}

    app = (config.get("icon") or {}).get("googlePlay")
    if not app:
        return [], {}

    def keep_last(reason: str | None) -> tuple[list[str], dict]:
        if reason:
            warnings.append(f"Game icon not refreshed ({reason}); kept the last one.")
        if previous.is_file() and str(record.get("source", "")).startswith("http"):
            shutil.copyfile(previous, target)
            return [], record
        return [], {}

    if fetcher is None:
        return keep_last(None)
    try:
        url = images.store_icon_url(app, fetcher)
        if url == record.get("source") and previous.is_file():
            shutil.copyfile(previous, target)
            return [], record
        original = fetcher.get(url, fresh=False)
        write_bytes(target, images.game_icon(original))
    except FetchError as error:
        return keep_last(str(error))
    except (OSError, ValueError) as error:
        return keep_last(f"the store's icon could not be read: {error}")
    return [], {"source": url, "sha256": hashlib.sha256(original).hexdigest()}


def variant_json(variant: assembler.Variant, image: str | None) -> dict:
    payload: dict = {
        "internalName": variant.name,
        "displayName": variant.display,
        "baseCharacterId": variant.parent,
        "isDefaultVariant": variant.parent is None,
    }
    if variant.aliases:
        payload["aliases"] = variant.aliases
    payload["modFilesName"] = variant.name
    if image:
        payload["image"] = image
    if variant.attributes:
        payload["attributes"] = variant.attributes
    if not variant.hashes:
        payload["hashesPending"] = True
    return payload


def image_checksums(folder: pathlib.Path) -> dict[str, str]:
    """Each picture in a pack's ``images/`` by file name, so the notes can say which portraits changed."""
    if not folder.is_dir():
        return {}
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}


def fingerprint(folder: pathlib.Path) -> str:
    """A checksum of everything in a pack except its manifest: "did the content change?"."""
    digest = hashlib.sha256()
    if not folder.is_dir():
        return ""
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.name != "manifest.json"):
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


_DATED = re.compile(r"^\d{4}\.\d{2}\.\d{2}(?:\.\d{1,3})?$")


def next_version(today: datetime.date, taken: set[str]) -> str:
    """``2026.09.25``, or ``2026.09.25.01`` for a second release the same day. Sorts as text.

    Always after every version in ``taken``, not only different from them: the app treats the
    version that sorts last as the newest. When the releases are deleted, the published list is
    empty and only the previous pack's own version is left in ``taken``; a free ``2026.09.26``
    after ``2026.09.26.01`` was published that way, and the app kept loading the older pack
    (2026-09-27).
    """
    base = today.strftime("%Y.%m.%d")

    # Only versions in the dated form order themselves this way: a tag like "v1" sorts after every
    # date as text, and made every later run refuse to publish (audit P7).
    newest = max((version for version in taken if _DATED.match(version or "")), default="")
    for candidate in [base, *(f"{base}.{number:02d}" for number in range(1, 100))]:
        if candidate not in taken and candidate > newest:
            return candidate
    if newest > f"{base}.99":
        raise BuildError(f"The newest version, {newest}, is later than today ({base}); the runner's clock or a version is wrong.")
    raise BuildError(f"More than 99 releases on {base}.")


def sources(config: dict, lock: dict) -> list[dict]:
    hash_config = config["hashes"]
    result = [
        {
            "kind": "hashes",
            "url": f"https://github.com/{hash_config['repo']}",
            **({"commit": lock["commit"]} if lock.get("commit") else {}),
            "license": hash_config.get("license", "See repository"),
        },
        {"kind": "roster", "url": config["roster"].get("url", ""), "license": config["roster"].get("license", "See source")},
        {"kind": "images", "url": config["roster"].get("imagesUrl", config["roster"].get("url", "")), "license": IMAGE_LICENCE},
    ]
    return result


def attribution(config: dict, pictures: dict, icon_source: str | None = None) -> str:
    hash_config = config["hashes"]
    hosts = sorted({str(p.get("source", "")).split("/")[2] for p in pictures.values() if str(p.get("source", "")).startswith("http")})
    manual_count = sum(1 for p in pictures.values() if str(p.get("source", "")).startswith("manual/"))
    lines = [
        "# Attribution",
        "",
        f"**Hashes** come from [{hash_config['repo']}](https://github.com/{hash_config['repo']}) "
        f"({hash_config.get('license', 'see the repository')}), its `{hash_config['path']}`, plus any added by hand in this repository's `manual/` folder.",
        "",
        f"**The character list** comes from {config['roster'].get('credit', config['roster'].get('url', 'the source in manifest.json'))}.",
        "",
        "**Portraits** are game art © Kuro Games, downloaded from "
        + (", ".join(hosts) if hosts else "no source")
        + (f", and {manual_count} added by hand" if manual_count else "")
        + ". This repository's `upstream/<game>/images.json` records each picture's exact address.",
        "",
        *(
            [
                "**The game icon** is game art © Kuro Games: "
                + ("the game's own app icon, from its Google Play page." if icon_source.startswith("http") else "kept by hand in this repository's `manual/` folder."),
                "",
            ]
            if icon_source
            else []
        ),
        "This pack is built by https://github.com/dotStray/wuwa-preset and is published under GPL-3.0.",
        "",
    ]
    return "\n".join(lines)

"""Publishing: pack zips, ``index.json``, and GitHub releases.

The app reads ``index.json`` from this repository's ``main`` branch, which lists the newest
versions of each game with its zip's address and checksum. A build that changes anything makes
**one** release, tagged with the day (``2026.09.26``, ``.01`` for a second that day), holding a
``<game>-<version>.zip`` for each game whose pack changed and notes saying what changed in each.
Every other game's current pack is attached to it again, so the newest release always has all of
them and nobody has to look back through older ones (the user's choice, 2026-09-27). That copy is
the published zip itself, downloaded and checked against ``index.json``'s checksum — not zipped
again, because the same files compress differently under another zlib (a runner's against
zlib-ng, measured). ``index.json`` then points at the newest copy, so an older release can be
deleted without taking a current pack with it.

A release is created *before* ``index.json`` points at it, so the app can never be told about
a zip that does not exist yet. Before publishing, every address in ``index.json`` is checked
against the releases that exist: one whose release was deleted is dropped, and a game whose
current pack was deleted is published again.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import zipfile
from dataclasses import dataclass, field
from typing import Protocol

from packbuilder.build import next_version
from packbuilder.files import BuildError, Repo, read_json, write_bytes, write_json, write_text
from packbuilder.reports import Changes, card_line, contents

KEEP_VERSIONS = 10

_DATED = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})(?:\.(\d{1,3}))?$")


def display_version(version: str | None) -> str:
    """A version as a person reads it: ``2026.09.25`` as ``26.9.25``, ``2026.09.25.01`` as ``26.9.25.1``.

    Only what is shown (the user's choice, 2026-09-27). Tags, file names and ``index.json`` keep the
    stored form: versions are ordered by comparing that text, which ``26.10.1`` against ``26.9.28``
    would get wrong. Anything not in the dated form is shown as it is.
    """
    if not version:
        return ""
    match = _DATED.match(version)
    if not match:
        return version
    year, month, day, build = match.groups()
    parts = [year[2:], str(int(month)), str(int(day))] + ([str(int(build))] if build else [])
    return ".".join(parts)
DEFAULT_REPOSITORY = "dotStray/wuwa-preset"


def zip_bytes(pack: pathlib.Path) -> bytes:
    manifest = read_json(pack / "manifest.json", {}) or {}
    stamp = _stamp(manifest.get("packVersion", "1980.01.01"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # No link, and nothing reached through one: a link in a pack could put .git/config or anything
        # else on this machine into a public zip (P9).
        files = (p for p in pack.rglob("*") if p.is_file() and not _through_link(pack, p))
        for path in sorted(files, key=lambda p: p.relative_to(pack).as_posix()):
            info = zipfile.ZipInfo(path.relative_to(pack).as_posix(), stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def _through_link(root: pathlib.Path, path: pathlib.Path) -> bool:
    """Whether ``path`` is a link, or is reached through one, below ``root``."""
    current = path
    while current != root and current != current.parent:
        if current.is_symlink():
            return True
        current = current.parent
    return False


def _stamp(version: str) -> tuple[int, int, int, int, int, int]:
    try:
        year, month, day = (int(p) for p in version.split(".")[:3])
        return (year, month, day, 0, 0, 0)
    except ValueError:
        return (1980, 1, 1, 0, 0, 0)


def index_entry(game: dict, manifest: dict, url: str, data: bytes, changelog: str) -> dict:
    return {
        "packVersion": manifest["packVersion"],
        "packSchemaVersion": manifest.get("packSchemaVersion", 1),
        "minAppVersion": manifest.get("minAppVersion", "0.1.0"),
        "url": url,
        "sha256": hashlib.sha256(data).hexdigest(),
        "sizeBytes": len(data),
        "changelog": changelog,
    }


def published_versions(index: dict, game: str) -> set[str]:
    for pack in index.get("packs", []):
        if pack.get("gameId") == game:
            return {v.get("packVersion", "") for v in pack.get("versions", [])}
    return set()


def update_index(index: dict, game: dict, entry: dict) -> dict:
    """``index`` with ``entry`` as ``game``'s newest version, keeping the last few."""
    packs = [p for p in index.get("packs", []) if p.get("gameId") != game["gameId"]]
    previous = next((p for p in index.get("packs", []) if p.get("gameId") == game["gameId"]), {})
    versions = [entry] + [v for v in previous.get("versions", []) if v.get("packVersion") != entry["packVersion"]]
    versions.sort(key=lambda v: v.get("packVersion", ""), reverse=True)
    pack = {"gameId": game["gameId"], "displayName": game.get("displayName", game["gameId"])}
    for key in ("shortName", "importer"):
        if game.get(key):
            pack[key] = game[key]
    pack["versions"] = versions[:KEEP_VERSIONS]
    packs.append(pack)
    packs.sort(key=lambda p: p["gameId"])
    updated = max(
        (v["packVersion"] for p in packs for v in p["versions"]),
        default="1970.01.01",
    )
    year, month, day = _stamp(updated)[:3]
    return {"schemaVersion": 1, "updatedAt": f"{datetime.date(year, month, day).isoformat()}T00:00:00Z", "packs": packs}


class Releases(Protocol):
    """The repository's GitHub releases: what exists, and making one."""

    def list(self) -> dict[str, set[str]]:
        """Every release's tag, with the names of the files it holds."""

    def create(self, tag: str, title: str, notes: str, assets: list[pathlib.Path]) -> None:
        """One release holding ``assets``, or nothing at all: a half-made release is removed."""

    def download(self, tag: str, name: str, destination: pathlib.Path) -> None:
        """Saves release ``tag``'s file ``name`` as ``destination``; :class:`BuildError` if it cannot."""


class GitHubReleases:
    """:class:`Releases` through the ``gh`` command, which the weekly run has."""

    def __init__(self, repository: str):
        self.repository = repository

    def list(self) -> dict[str, set[str]]:
        finished = subprocess.run(
            ["gh", "api", f"repos/{self.repository}/releases", "--paginate", "--jq", ".[] | {tag: .tag_name, assets: [.assets[].name]}"],
            capture_output=True,
            text=True,
        )
        if finished.returncode != 0:
            raise BuildError(f"Could not read the list of releases: {finished.stderr.strip() or finished.stdout.strip()}")
        releases: dict[str, set[str]] = {}
        for line in finished.stdout.splitlines():
            if line.strip():
                item = json.loads(line)
                releases[item["tag"]] = set(item["assets"])
        return releases

    def create(self, tag: str, title: str, notes: str, assets: list[pathlib.Path]) -> None:
        notes_file = assets[0].parent / f"{tag}.md"
        write_text(notes_file, notes)
        command = ["gh", "release", "create", tag, *map(str, assets), "--repo", self.repository, "--title", title, "--notes-file", str(notes_file)]
        finished = subprocess.run(command, capture_output=True, text=True)
        if finished.returncode != 0:
            subprocess.run(["gh", "release", "delete", tag, "--repo", self.repository, "--cleanup-tag", "--yes"], capture_output=True, text=True)
            raise BuildError(f"Could not publish the {tag} release: {finished.stderr.strip() or finished.stdout.strip()}")

    def download(self, tag: str, name: str, destination: pathlib.Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = ["gh", "release", "download", tag, "--repo", self.repository, "--pattern", name, "--output", str(destination), "--clobber"]
        finished = subprocess.run(command, capture_output=True, text=True)
        if finished.returncode != 0:
            raise BuildError(f"Could not download {name} from the {tag} release: {finished.stderr.strip() or finished.stdout.strip()}")


@dataclass
class Published:
    """One build's release: its tag, and each game in it with its version."""

    tag: str
    games: list[str] = field(default_factory=list)
    not_attached: list[str] = field(default_factory=list)  # current packs that could not be attached again, and why

    def __str__(self) -> str:
        text = f"release {self.tag}: " + ", ".join(self.games)
        return text + "".join(f"\n   not attached again: {problem}" for problem in self.not_attached)


def reachable(url: str, repository: str, releases: dict[str, set[str]]) -> bool:
    """False only for an address in this repository's releases whose release or file is gone."""
    prefix = f"https://github.com/{repository}/releases/download/"
    if not url.startswith(prefix):
        return True
    tag, _, name = url[len(prefix):].partition("/")
    return name in releases.get(tag, set())


def prune(index: dict, repository: str, releases: dict[str, set[str]]) -> dict:
    """``index`` without the versions whose release was deleted; a game left with none is dropped."""
    packs = []
    for pack in index.get("packs", []):
        versions = [v for v in pack.get("versions", []) if reachable(v.get("url", ""), repository, releases)]
        if versions:
            packs.append({**pack, "versions": versions})
    return {**index, "packs": packs}


def publish(
    repo: Repo,
    games: list[str],
    changes: dict[str, Changes],
    *,
    releases: Releases,
    today: datetime.date,
    stopped: list[str] | None = None,
    repository: str | None = None,
) -> Published | None:
    """Releases, together, every game whose current pack is not published yet. None when there is none."""
    repository = repository or os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPOSITORY
    existing = releases.list()
    original = read_json(repo.index, {}) or {}
    index = prune(original, repository, existing)

    ready = []
    for game_id in games:
        manifest = read_json(repo.pack(game_id) / "manifest.json")
        game = read_json(repo.pack(game_id) / "game.json")
        if manifest and game and manifest["packVersion"] not in published_versions(index, game_id):
            ready.append((game_id, game, manifest))

    # A game whose build stopped still has its last good pack in packs/ — a stopped build never
    # touches it. When that pack is not published any more (its release was deleted), it is published
    # again rather than the game vanishing from index.json until its sources recover.
    restored = []
    for game_id in stopped or []:
        manifest = read_json(repo.pack(game_id) / "manifest.json")
        game = read_json(repo.pack(game_id) / "game.json")
        if manifest and game and manifest["packVersion"] not in published_versions(index, game_id):
            restored.append((game_id, game, manifest))

    if not ready and not restored:
        if index != original:
            write_json(repo.index, index)
        return None

    tag = next_version(today, set(existing))
    dist = repo.root / ".dist"
    assets, entries, sections = [], [], []
    for game_id, game, manifest in ready:
        version = manifest["packVersion"]
        name = f"{game_id}-{version}.zip"
        data = zip_bytes(repo.pack(game_id))
        write_bytes(dist / name, data)
        assets.append(dist / name)
        change = changes.get(game_id) or Changes()
        if not change.contents:
            change.contents = contents(read_json(repo.pack(game_id) / "variants.json", []) or [])
        url = f"https://github.com/{repository}/releases/download/{tag}/{name}"
        entries.append((game, index_entry(game, manifest, url, data, card_line(read_json(repo.pack(game_id) / "variants.json", []) or []))))
        sections.append(_section(game, version, change))
    for game_id, game, manifest in restored:
        version = manifest["packVersion"]
        # Zipped again, not downloaded: a restored pack is one whose release was deleted, so there is
        # no published copy of it left to attach (checked for the audit's P9, 2026-09-28).
        name = f"{game_id}-{version}.zip"
        data = zip_bytes(repo.pack(game_id))
        write_bytes(dist / name, data)
        assets.append(dist / name)
        url = f"https://github.com/{repository}/releases/download/{tag}/{name}"
        variants = read_json(repo.pack(game_id) / "variants.json", []) or []
        entries.append((game, index_entry(game, manifest, url, data, card_line(variants))))
        sections.append(_stopped_restored(repo, game_id, index, version))
    ready_ids = {game_id for game_id, _, _ in ready}
    restored_ids = {game_id for game_id, _, _ in restored}
    not_attached: list[str] = []

    def attach_again(game_id: str) -> bool:
        try:
            path, pack, entry = _attach_again(game_id, index, releases, repository, tag, dist)
        except BuildError as error:
            not_attached.append(f"{game_id}: {error}")
            return False
        assets.append(path)
        entries.append((pack, entry))
        return True

    for game_id in games:
        if game_id not in ready_ids:
            sections.append(_unchanged(repo, game_id, index, attach_again(game_id)))
    for game_id in stopped or []:
        if game_id not in restored_ids:
            sections.append(_stopped(repo, game_id, index, attach_again(game_id)))

    names = [_display(repo, game_id, index) for game_id in [*games, *(stopped or [])]]
    releases.create(tag, f"{'Pack' if len(names) == 1 else 'Packs'} {display_version(tag)}", _notes(sections, names, repository), assets)
    for game, entry in entries:
        index = update_index(index, game, entry)
    write_json(repo.index, index)
    published = [f"{game_id} {display_version(manifest['packVersion'])}" for game_id, _, manifest in ready]
    published += [f"{game_id} {display_version(manifest['packVersion'])} (its last good pack, again)" for game_id, _, manifest in restored]
    return Published(tag, published, not_attached)


def _attach_again(game_id: str, index: dict, releases: Releases, repository: str, tag: str, dist: pathlib.Path) -> tuple[pathlib.Path, dict, dict]:
    """A game's current pack for this release: the published zip, downloaded, and its index entry at the new address.

    :class:`BuildError` when there is none, it cannot be downloaded, or it is not the file
    ``index.json`` describes — attaching that under the same version would be a different pack
    the app cannot tell apart.
    """
    pack = next((p for p in index.get("packs", []) if p.get("gameId") == game_id), None)
    if not pack or not pack.get("versions"):
        raise BuildError("no current pack is published")
    current = pack["versions"][0]
    prefix = f"https://github.com/{repository}/releases/download/"
    url = current.get("url", "")
    if not url.startswith(prefix):
        raise BuildError(f"its current pack is not in this repository's releases ({url})")
    old_tag, _, name = url[len(prefix):].partition("/")
    path = dist / name
    releases.download(old_tag, name, path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != current.get("sha256"):
        path.unlink()
        raise BuildError(f"{name} in the {old_tag} release is not the file index.json describes (its checksum differs)")
    return path, pack, {**current, "url": f"{prefix}{tag}/{name}"}


def _display(repo: Repo, game_id: str, index: dict) -> str:
    game = read_json(repo.pack(game_id) / "game.json", {}) or {}
    listed = next((p for p in index.get("packs", []) if p.get("gameId") == game_id), {})
    return game.get("displayName") or listed.get("displayName") or game_id


def _current(game_id: str, index: dict) -> str | None:
    listed = next((p for p in index.get("packs", []) if p.get("gameId") == game_id), {})
    versions = listed.get("versions", [])
    return versions[0]["packVersion"] if versions else None


def _section(game: dict, version: str, change: Changes) -> str:
    lines = [f"## {game.get('displayName', game['gameId'])} {display_version(version)}", ""]
    if change.any():
        lines += [f"- {line}" for line in change.lines()]
        if not change.first:
            lines += ["", f"The pack now has {change.contents}."]
    else:
        lines.append(f"The pack has {change.contents}.")
    return "\n".join(lines)


def _still(current: str | None, attached: bool) -> str:
    return f"the current pack is still {display_version(current)}" + (", attached again" if attached else "") if current else ""


def _unchanged(repo: Repo, game_id: str, index: dict, attached: bool) -> str:
    still = _still(_current(game_id, index), attached)
    return f"## {_display(repo, game_id, index)}\n\nNo change" + (f"; {still}." if still else ".")


def _stopped(repo: Repo, game_id: str, index: dict, attached: bool) -> str:
    still = _still(_current(game_id, index), attached)
    kept = f" {still[0].upper()}{still[1:]}." if still else ""
    return f"## {_display(repo, game_id, index)}\n\nNot updated this time: the build stopped, and `reports/{game_id}/blocked.md` says why.{kept}"


def _stopped_restored(repo: Repo, game_id: str, index: dict, version: str) -> str:
    return (
        f"## {_display(repo, game_id, index)} {display_version(version)}\n\nNot updated this time: the build stopped, "
        f"and `reports/{game_id}/blocked.md` says why. Its last good pack, {display_version(version)}, is published again."
    )


def _notes(sections: list[str], names: list[str], repository: str) -> str:
    """The release's text: what it holds, where the README says how to install it, then each game's section."""
    install = f"[Installing it](https://github.com/{repository}#installing-it)"
    if len(names) == 1:
        intro = (
            f"The {names[0]} Game Pack. The mod manager downloads and installs it by itself: see {install}. "
            "To download it by hand, it is the zip under Assets below."
        )
    else:
        intro = (
            f"Game Packs for {', '.join(names)}. The mod manager downloads and installs them by itself: see {install}. "
            "To download one by hand, each game's pack is its zip under Assets below."
        )
    return "\n\n".join([intro, *sections]) + "\n"


def local_registry(repo: Repo, games: list[str], destination: pathlib.Path) -> pathlib.Path:
    """A folder XXSM can use as a registry, for trying packs before they are published."""
    index: dict = {}
    for game_id in games:
        pack = repo.pack(game_id)
        manifest = read_json(pack / "manifest.json")
        game = read_json(pack / "game.json")
        if not manifest or not game:
            continue
        name = f"{game_id}-{manifest['packVersion']}.zip"
        data = zip_bytes(pack)
        write_bytes(destination / "packs" / name, data)
        index = update_index(index, game, index_entry(game, manifest, f"packs/{name}", data, "Built on this machine, not published."))
    write_json(destination / "index.json", index)
    return destination

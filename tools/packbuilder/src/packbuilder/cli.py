"""``packbuilder`` — the commands.

    packbuilder build [--game G] [--roster-only | --hashes-only] [--no-network] [--publish]
    packbuilder try [--game G] [--out DIR] [--no-network]
    packbuilder learn GAME CHARACTER MOD_FOLDER_OR_ZIP [--dry-run]
    packbuilder publish [--message TEXT]

``build`` is what the weekly run does. ``try`` builds a copy on this machine and makes a
folder XXSM can install from, without touching the repository. ``learn`` takes a
character's hashes from a mod into ``manual/``. ``publish`` commits and pushes the
``manual/``, ``overrides/`` and ``config/`` changes (it needs write access); the run on GitHub then
builds and publishes them.
"""

from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from packbuilder import learn as learner, release
from packbuilder.build import build_game
from packbuilder.files import GAMES, BuildError, Repo, read_json
from packbuilder.http import Fetcher
from packbuilder.reports import plural

DEFAULT_TRY = pathlib.Path(os.environ.get("XDG_DATA_HOME", pathlib.Path.home() / ".local" / "share")) / "wuwa-preset-try"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="packbuilder", description="Builds the XXSM Game Packs in this repository.")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="Refresh the sources and rebuild the packs.")
    _game_options(build)
    stage = build.add_mutually_exclusive_group()
    stage.add_argument("--roster-only", action="store_true", help="Refresh the character lists, not the hashes.")
    stage.add_argument("--hashes-only", action="store_true", help="Refresh the hashes, not the character lists.")
    build.add_argument("--publish", action="store_true", help="Release every pack that changed and update index.json (the weekly run).")

    trial = commands.add_parser("try", help="Build a copy and make a folder XXSM can install from. Changes nothing here.")
    _game_options(trial)
    trial.add_argument("--out", type=pathlib.Path, default=DEFAULT_TRY, help=f"Where to put it (default {DEFAULT_TRY}).")

    learn = commands.add_parser("learn", help="Take a character's hashes out of a mod into manual/.")
    learn.add_argument("game", choices=GAMES)
    learn.add_argument("character", help="The character's name in the pack, like LuukHerssen.")
    learn.add_argument("source", type=pathlib.Path, help="The mod's folder, or a .zip of it.")
    learn.add_argument("--dry-run", action="store_true", help="Say what would be added, and add nothing.")

    publish = commands.add_parser("publish", help="Commit and push the manual/, overrides/ and config/ changes (needs write access).")
    publish.add_argument("--message", "-m", help="What the change is, for the history.")

    args = parser.parse_args(argv)
    repo = Repo.find(pathlib.Path.cwd()) if (pathlib.Path.cwd() / "config").is_dir() else Repo.find()
    try:
        if args.command == "build":
            return _build(repo, args)
        if args.command == "try":
            return _try(repo, args)
        if args.command == "learn":
            return _learn(repo, args)
        return _publish(repo, args)
    except BuildError as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1


def _game_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--game", action="append", choices=GAMES, help="Only this game. May be given more than once. Default: all.")
    parser.add_argument("--no-network", action="store_true", help="Build from the saved copies and the cache only.")
    parser.add_argument("--date", type=datetime.date.fromisoformat, help=argparse.SUPPRESS)


def _fetcher(repo: Repo, args) -> Fetcher | None:
    if args.no_network:
        return None
    return Fetcher(repo.cache, token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))


def _run_builds(repo: Repo, args) -> tuple[list, bool]:
    games = args.game or list(GAMES)
    index = read_json(repo.index, {}) or {}
    fetcher = _fetcher(repo, args)
    results = []
    for game in games:
        print(f"== {game}", flush=True)
        result = build_game(
            repo,
            game,
            fetcher=fetcher,
            refresh_roster=not getattr(args, "hashes_only", False),
            refresh_hashes=not getattr(args, "roster_only", False),
            today=args.date,
            known_versions=release.published_versions(index, game),
        )
        results.append(result)
        for warning in result.warnings:
            print(f"   note: {warning}")
        if result.errors:
            print(f"   STOPPED — nothing for {game} was changed:")
            for error in result.errors:
                print(f"   - {error}")
        else:
            if result.changed:
                print(f"   new version {release.display_version(result.version)}. " + " ".join(result.summary))
            else:
                print(f"   no changes; still {release.display_version(result.version)}.")
    return results, all(not r.errors for r in results)


def _build(repo: Repo, args) -> int:
    results, ok = _run_builds(repo, args)
    published = None
    if args.publish:
        repository = os.environ.get("GITHUB_REPOSITORY") or release.DEFAULT_REPOSITORY
        published = release.publish(
            repo,
            [r.game for r in results if not r.errors],
            {r.game: r.changes for r in results if r.changes},
            releases=release.GitHubReleases(repository),
            today=args.date or datetime.datetime.now(datetime.timezone.utc).date(),
            stopped=[r.game for r in results if r.errors],
            repository=repository,
        )
        print(f"published {published}" if published else "nothing new to publish")
    _step_summary(results, published)
    return 0 if ok else 1


def _try(repo: Repo, args) -> int:
    with tempfile.TemporaryDirectory(prefix="wuwa-preset-try-") as scratch:
        copy = pathlib.Path(scratch) / "repo"
        shutil.copytree(repo.root, copy, ignore=shutil.ignore_patterns(".git", ".cache", ".dist", "__pycache__"))
        cache = repo.cache
        trial = Repo(copy)
        object.__setattr__(trial, "root", copy)
        fetcher = None if args.no_network else Fetcher(cache, token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
        games = args.game or list(GAMES)
        ok = True
        for game in games:
            result = build_game(trial, game, fetcher=fetcher, today=args.date)
            ok &= not result.errors
            print(f"== {game}: " + ("; ".join(result.errors) if result.errors else " ".join(result.summary)))
        if not ok:
            print("Nothing was made: fix what is listed above first.", file=sys.stderr)
            return 1
        if args.out.exists():
            # Only a folder this command made (it holds an index.json) or an empty one (P9).
            if not ((args.out / "index.json").is_file() or not any(args.out.iterdir())):
                print(f"{args.out} is not a folder `packbuilder try` made and is not empty; nothing was changed. Choose another --out.", file=sys.stderr)
                return 1
            shutil.rmtree(args.out)
        release.local_registry(trial, games, args.out)
    print()
    print(f"Ready to try in XXSM. Add this folder as a pack source: {args.out}")
    print(f"  from the command line:  xxsm config registry add {args.out}")
    print("Nothing in the repository changed, and nothing was published.")
    return 0


def _learn(repo: Repo, args) -> int:
    learned = learner.learn(repo.manual(args.game), repo.pack(args.game), args.character, args.source.expanduser(), dry_run=args.dry_run)
    name = learned.target.stem
    print(f"Read {plural(learned.files, '.ini file')} from {args.source}.")
    if learned.shaders:
        print(f"Left out {plural(learned.shaders, 'shader hash', 'shader hashes')} (every character has those).")
    if learned.elsewhere:
        print(f"Left out {plural(len(learned.elsewhere), 'hash', 'hashes')} another character already has:")
        for value, others in sorted(learned.elsewhere.items()):
            print(f"  {value}  {', '.join(others)}")
    if learned.already:
        print(f"{plural(learned.already, 'hash', 'hashes')} {'was' if learned.already == 1 else 'were'} already there.")
    if not learned.added:
        print(f"Nothing new to add for {name}.")
        return 0
    verb = "Would add" if args.dry_run else "Added"
    print(f"{verb} {plural(len(learned.added), 'hash', 'hashes')} for {name}" + ("" if args.dry_run else f" to {learned.target.relative_to(repo.root)}") + ":")
    for entry in learned.added:
        print(f"  {entry['kind']:12} {entry['hash']}")
    if not args.dry_run:
        print("Try it with `packbuilder try`, and send it with `packbuilder publish`.")
    return 0


def _publish(repo: Repo, args) -> int:
    def git(*command: str, check: bool = True) -> subprocess.CompletedProcess:
        finished = subprocess.run(["git", "-C", str(repo.root), *command], capture_output=True, text=True)
        if check and finished.returncode != 0:
            raise BuildError(f"git {' '.join(command)} failed: {finished.stderr.strip() or finished.stdout.strip()}")
        return finished

    paths = [p for p in ("manual", "overrides", "config") if (repo.root / p).exists()]
    git("add", "--", *paths)
    staged = git("diff", "--cached", "--name-status", "--", *paths).stdout.strip()
    if not staged:
        print("Nothing in manual/, overrides/ or config/ has changed; nothing to send.")
        return 0
    print("Sending:")
    print("\n".join(f"  {line}" for line in staged.splitlines()))
    message = args.message or "manual: hand additions"
    git("commit", "-m", message, "--", *paths)
    git("pull", "--rebase", "--autostash")
    git("push")
    print()
    print("Sent. GitHub now builds and publishes the packs; if anything is wrong with them, the run stops and the maintainers are notified.")
    return 0


def _step_summary(results, published) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["# Pack build", ""]
    for result in results:
        if result.errors:
            lines.append(f"## {result.game}: stopped, nothing published")
            lines += [f"- {e}" for e in result.errors]
        else:
            shown = release.display_version(result.version)
            lines.append(f"## {result.game}: " + (f"new version {shown}" if result.changed else f"no changes; still {shown}"))
            lines += [f"- {s}" for s in result.summary] if result.changed else []
        lines += [f"- note: {w}" for w in result.warnings]
        lines.append("")
    if published:
        lines.append(f"Published {published}")
    with open(path, "a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")

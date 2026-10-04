"""Where everything lives in the repository, and how files are written.

Every generated file is written the same way — two-space JSON, keys in the order given,
a trailing newline, UTF-8 without escaping — so that the same data always produces the
same bytes and a diff between two builds shows only what changed.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import tempfile
from dataclasses import dataclass

GAMES = ("wuwa",)


@dataclass(frozen=True)
class Repo:
    """This repository on disk."""

    root: pathlib.Path

    @staticmethod
    def find(start: pathlib.Path | None = None) -> "Repo":
        """The repository containing ``start`` (default: this file), found by its ``config/`` folder."""
        here = (start or pathlib.Path(__file__)).resolve()
        for candidate in [here, *here.parents]:
            if (candidate / "config").is_dir() and (candidate / "tools" / "packbuilder").is_dir():
                return Repo(candidate)
        raise SystemExit(
            "Could not find the repository (a folder holding config/ and tools/packbuilder/). "
            "Run packbuilder from inside it."
        )

    def config(self, game: str) -> pathlib.Path:
        return self.root / "config" / f"{game}.json"

    def overrides(self, game: str) -> pathlib.Path:
        return self.root / "overrides" / f"{game}.json"

    def manual(self, game: str) -> pathlib.Path:
        return self.root / "manual" / game

    def upstream(self, game: str) -> pathlib.Path:
        return self.root / "upstream" / game

    def ledger(self, game: str) -> pathlib.Path:
        return self.root / "ledger" / f"{game}.json"

    def pack(self, game: str) -> pathlib.Path:
        return self.root / "packs" / game

    def reports(self, game: str) -> pathlib.Path:
        return self.root / "reports" / game

    @property
    def index(self) -> pathlib.Path:
        return self.root / "index.json"

    @property
    def cache(self) -> pathlib.Path:
        return self.root / ".cache"


def dumps(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def write_text(path: pathlib.Path, text: str) -> bool:
    """Writes atomically. Returns whether the file changed."""
    return write_bytes(path, text.encode("utf-8"))


def write_json(path: pathlib.Path, payload: object) -> bool:
    return write_text(path, dumps(payload))


def write_bytes(path: pathlib.Path, data: bytes) -> bool:
    if path.is_file() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise
    return True


def read_json(path: pathlib.Path, default: object = None) -> object:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise BuildError(f"{path} is not valid JSON: {error.msg} at line {error.lineno}, column {error.colno}.") from error


class BuildError(Exception):
    """A problem a person has to fix. The message is shown as it is, so it is written in plain words."""


def swap_in(staging: pathlib.Path, destination: pathlib.Path) -> None:
    """Puts a finished folder in place of another: the old one renamed aside first, removed last.

    Removing the old folder first left a moment with no folder at all; a run stopped there came
    back to find the pack missing, and the "a character would disappear" check then compared
    against nothing (audit P8).
    """
    aside = destination.with_name(f".{destination.name}.old")
    if aside.exists():
        shutil.rmtree(aside)
    if destination.exists():
        destination.rename(aside)
    staging.rename(destination)
    if aside.exists():
        shutil.rmtree(aside)

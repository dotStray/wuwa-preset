"""A small fake repository for tests: two characters, one outfit, no network."""

from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402

from packbuilder.files import Repo  # noqa: E402

CONFIG = {
    "gameId": "testgame",
    "displayName": "Test Game",
    "shortName": "TG",
    "importer": "TGMI",
    "hashes": {"repo": "someone/TG-Fixer", "path": "config.json", "license": "GPL-3.0"},
    "roster": {
        "source": "nanoka",
        "url": "https://example.invalid",
        "dataUrl": "https://data.example.invalid",
        "game": "tg",
        "imagesUrl": "https://data.example.invalid/assets/tg",
        "credit": "a test",
    },
    "portraits": {"crop": "none"},
    "attributes": {
        "element": {"displayName": "Element", "values": [{"id": "cryo", "displayName": "Cryo", "from": ["Ice"]}], "ignore": ["None"]},
        "rarity": {"displayName": "Rarity", "kind": "number"},
    },
}


def component(main: str, texture: str = "", old: list[str] | None = None) -> dict:
    """One entry of the hash source, as Moonholder writes it: a model's main hash and a texture."""
    entry: dict = {"main_hashes": [{"old": list(old or []), "new": main}]}
    if texture:
        entry["textures"] = {texture: {"meta": {"id": 0, "type": "D"}}}
    return entry


def png(colour=(200, 100, 50, 255), size=(64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


class FakeRepo:
    """A temporary repository with a saved roster and hash copy, so builds run offline."""

    def __init__(self, game: str = "testgame"):
        self._dir = tempfile.TemporaryDirectory(prefix="packbuilder-test-")
        self.root = pathlib.Path(self._dir.name)
        (self.root / "tools" / "packbuilder").mkdir(parents=True)
        self.game = game
        self.repo = Repo(self.root)
        self.write(f"config/{game}.json", {**CONFIG, "gameId": game})
        self.set_roster(
            [
                {"key": "avatar:1", "name": "Ganyu", "joinKeys": ["ganyu"], "attributes": {"element": "Ice", "rarity": 5}},
                {"key": "avatar:2", "name": "Lan Yan", "joinKeys": ["lanyan"], "attributes": {"element": "Ice", "rarity": 4}},
            ]
        )
        self.set_folders({"Ganyu": component("1575ec63", "6d78ac96"), "GanyuTwilight": component("aaaa0001", "6d78ac96")})
        self.set_outfits("avatar:1", [{"key": "skin:11", "name": "Twilight Blossom", "image": None}])

    def cleanup(self) -> None:
        self._dir.cleanup()

    def write(self, relative: str, payload) -> pathlib.Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        elif isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def read(self, relative: str):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def set_roster(self, characters: list[dict]) -> None:
        self.write(f"upstream/{self.game}/roster.json", {"source": "nanoka", "characters": characters})

    def set_outfits(self, key: str, outfits: list[dict]) -> None:
        saved = self.read(f"upstream/{self.game}/roster.json")
        for character in saved["characters"]:
            if character["key"] == key:
                character["outfits"] = outfits
        self.write(f"upstream/{self.game}/roster.json", saved)

    def set_folders(self, folders: dict[str, dict]) -> None:
        """The hash source's saved copy: Moonholder's config.json with these entries."""
        self.write(f"upstream/{self.game}/hashes/config.json", {"version": {}, "characters": folders})
        self.write(f"upstream/{self.game}/hashes/lock.json", {"repo": "someone/TG-Fixer", "commit": "c0ffee", "path": "config.json", "sha": "0" * 40})

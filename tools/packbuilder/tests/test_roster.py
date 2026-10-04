"""The character list from nanoka: its files, outfits and pictures, and what is kept when it fails."""

from __future__ import annotations

import json
import unittest

import helpers  # noqa: F401  (puts src on the path)

from packbuilder import roster
from packbuilder.http import FetchError

DATA = "https://static.nanoka.cc"
SETTINGS = {"dataUrl": DATA, "game": "ww", "imagesUrl": f"{DATA}/assets/ww"}
ICON = "/Game/Aki/UI/UIResources/Common/Image/IconRoleHead256/T_IconRoleHead256_{}_UI.T_IconRoleHead256_{}_UI"
CARD = "/Game/Aki/UI/UIResources/Common/Image/IconRolePile/T_IconRole_Pile_{}_UI.T_IconRole_Pile_{}_UI"


def entry(name: str, icon: int, element: int = 1, weapon: int = 2, rank: int = 5) -> dict:
    return {"icon": ICON.format(icon, icon), "rank": rank, "weapon": weapon, "element": element, "en": name, "zh": "?"}


def page(*looks: tuple[int, str, int]) -> dict:
    """A character's page with these looks: (number, outfit name, nanoka's sort index)."""
    return {"id": 1, "skin": {str(n): {"id": n, "title_name": title, "sort_index": order, "background": CARD.format(n, n)} for n, title, order in looks}}


class FakeSite:
    """Answers by address; a missing file is one that cannot be read. Remembers what was asked."""

    def __init__(self, files: dict):
        self.files = files
        self.asked: list[str] = []

    def get_json(self, url, *, fresh=True):
        self.asked.append(url)
        answer = self.files.get(url.removeprefix(DATA))
        if answer is None:
            raise FetchError(f"{url}: HTTP 404")
        return answer


def site(latest: str = "3.7", live: str = "3.7", listing: dict | None = None, pages: dict | None = None, live_listing: dict | None = None) -> FakeSite:
    listing = listing if listing is not None else {"1102": entry("Sanhua", 7), "1107": entry("Carlotta", 32)}
    files = {"/manifest.json": {"gi": {"latest": "7.1"}, "ww": {"latest": latest, "live": live}}, f"/ww/{latest}/character.json": listing}
    if live_listing is not None:
        files[f"/ww/{live}/character.json"] = live_listing
    for number in listing:
        files[f"/ww/{latest}/en/character/{number}.json"] = (pages or {}).get(number, page((int(f"8100{number}"), "Original", 0)))
    return FakeSite(files)


class ReadTest(unittest.TestCase):
    def test_every_character_is_read_with_its_name_attributes_and_face_icon(self):
        found = roster.read(site(), [], SETTINGS)
        sanhua = next(c for c in found.characters if c.name == "Sanhua")
        self.assertEqual(sanhua.key, "avatar:1102")
        self.assertEqual(sanhua.attributes, {"element": 1, "weaponClass": 2, "rarity": 5})
        self.assertEqual(sanhua.image, f"{DATA}/assets/ww/UIResources/Common/Image/IconRoleHead256/T_IconRoleHead256_7_UI.webp")
        self.assertEqual(sanhua.join_keys, ["sanhua"])
        self.assertEqual(found.left_out, [])

    def test_outfits_are_every_look_but_the_one_sorted_first_and_show_their_card_s_top_square(self):
        pages = {"1107": page((81011107, "Splashing Summer", 10), (81001107, "Trendsetter", 0))}
        carlotta = next(c for c in roster.read(site(pages=pages), [], SETTINGS).characters if c.name == "Carlotta")
        self.assertEqual([(o.key, o.name, o.crop) for o in carlotta.outfits], [("skin:81011107", "Splashing Summer", "top-square")])
        self.assertTrue(carlotta.outfits[0].image.endswith("/IconRolePile/T_IconRole_Pile_81011107_UI.webp"))

    def test_a_character_only_the_newer_version_has_is_in_marked_only_in_the_list(self):
        listing = {"1102": entry("Sanhua", 7), "1311": entry("Hsin", 80)}
        found = roster.read(site(latest="3.8.1", live="3.7", listing=listing, live_listing={"1102": entry("Sanhua", 7)}), [], SETTINGS)
        self.assertEqual({c.name: c.branch for c in found.characters}, {"Sanhua": "release", "Hsin": "beta"})

    def test_an_entry_with_no_name_is_reported_and_a_name_found_once_is_kept(self):
        listing = {"1102": entry("", 7), "1999": entry("", 9)}
        previous = [roster.Character(key="avatar:1102", name="Sanhua", join_keys=["sanhua"])]
        found = roster.read(site(listing=listing), previous, SETTINGS)
        self.assertEqual([c.name for c in found.characters], ["Sanhua"])
        self.assertEqual([(i.key, "no name" in i.reason) for i in found.left_out], [("avatar:1999", True)])

    def test_a_page_is_read_again_only_when_the_version_or_the_character_s_entry_changes(self):
        first = roster.read(site(), [], SETTINGS)
        again = site()
        roster.read(again, first.characters, SETTINGS)
        self.assertFalse([u for u in again.asked if "/en/character/" in u])
        newer = site(latest="3.8", live="3.8")
        roster.read(newer, first.characters, SETTINGS)
        self.assertEqual(len([u for u in newer.asked if "/en/character/" in u]), 2)
        changed = site(listing={"1102": entry("Sanhua", 8), "1107": entry("Carlotta", 32)})
        roster.read(changed, first.characters, SETTINGS)
        self.assertEqual([u for u in changed.asked if "/en/character/" in u], [f"{DATA}/ww/3.7/en/character/1102.json"])

    def test_a_page_that_cannot_be_read_keeps_last_run_s_outfits_and_says_so(self):
        pages = {"1107": page((81001107, "Trendsetter", 0), (81011107, "Splashing Summer", 10))}
        first = roster.read(site(pages=pages), [], SETTINGS)
        down = site(latest="3.8", live="3.8")
        down.files.pop("/ww/3.8/en/character/1107.json")
        found = roster.read(down, first.characters, SETTINGS)
        carlotta = next(c for c in found.characters if c.name == "Carlotta")
        self.assertEqual([o.name for o in carlotta.outfits], ["Splashing Summer"])
        self.assertIsNone(carlotta.source_hash, "read again next run")
        self.assertTrue(any("Carlotta's page was not read" in w and "kept" in w for w in found.warnings))

    def test_the_list_down_is_a_fetch_error_so_the_whole_last_list_is_kept(self):
        broken = site()
        broken.files.pop("/ww/3.7/character.json")
        with self.assertRaises(FetchError):
            roster.read(broken, [], SETTINGS)
        with self.assertRaises(FetchError):
            roster.read(FakeSite({"/manifest.json": {"gi": {"latest": "7.1"}}}), [], SETTINGS)
        with self.assertRaises(FetchError):
            roster.read(site(listing={}), [], SETTINGS)

    def test_a_picture_is_only_ever_a_path_in_the_game_s_files_on_nanoka(self):
        images = SETTINGS["imagesUrl"]
        self.assertIsNone(roster.asset_url(images, "https://evil.tld/x.webp"))
        self.assertIsNone(roster.asset_url(images, "/Game/Aki/UI/../../etc/passwd"))
        self.assertIsNone(roster.asset_url(images, None))
        self.assertEqual(roster.asset_url(images, "/Game/Aki/UI/A/B.B"), f"{images}/A/B.webp")

    def test_a_saved_list_reads_back_as_it_was_written(self):
        found = roster.read(site(pages={"1107": page((1, "Own", 0), (2, "Splashing Summer", 10))}), [], SETTINGS)
        again = [roster.Character.from_json(json.loads(json.dumps(c.to_json()))) for c in found.characters]
        self.assertEqual(again, found.characters)


class ConfigTest(unittest.TestCase):
    """The game's settings: nanoka's numbers for attribute and weapon, in the game's own words."""

    REPO = helpers.pathlib.Path(__file__).resolve().parents[3]

    def test_nanoka_s_numbers_map_to_the_game_s_names(self):
        config = json.loads((self.REPO / "config" / "wuwa.json").read_text())
        table = {a: {str(w): v["id"] for v in spec["values"] for w in v["from"]} for a, spec in config["attributes"].items() if spec.get("values")}
        self.assertEqual(table["element"], {"1": "glacio", "2": "fusion", "3": "electro", "4": "aero", "5": "spectro", "6": "havoc"})
        self.assertEqual(table["weaponClass"], {"1": "broadblade", "2": "sword", "3": "pistols", "4": "gauntlets", "5": "rectifier"})
        self.assertEqual(config["attributes"]["rarity"]["kind"], "number")


if __name__ == "__main__":
    unittest.main()

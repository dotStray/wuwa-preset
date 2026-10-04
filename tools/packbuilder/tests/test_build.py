"""Whole builds in a fake repository, offline: joining, manual/, stability, and the stops."""

from __future__ import annotations

import datetime
import pathlib
import unittest
from unittest import mock

import helpers

from packbuilder import assemble, build, hashes, learn, manual, roster
from packbuilder.build import build_game
from packbuilder.hashes import Folder
from packbuilder.settings import Overrides

DAY = datetime.date(2026, 9, 25)


def character(key, name, *codes, **attributes):
    return roster.Character(key=key, name=name, **roster.keys_for(name, *codes), attributes=attributes)


def folder(name, main, texture=""):
    return Folder(name, name, hashes.components(helpers.component(main, texture))[0])


class AssembleTest(unittest.TestCase):
    def run_it(self, characters, folders, overrides=None, ledger=None, hand=None):
        return assemble.assemble(helpers.CONFIG, overrides or Overrides(), characters, folders, ledger or {}, hand or manual.Manual())

    def by_name(self, result):
        return {v.name: v for v in result.variants}

    def test_a_hand_added_picture_gives_way_once_the_source_has_one_unless_kept(self):
        # The user's choice (2026-09-27): no reminder to delete it; the source's simply takes over.
        with_picture = roster.Character(key="a:1", name="Hyacine", join_keys=["hyacine"], image="source-picture")
        without = roster.Character(key="a:2", name="Belle", join_keys=["belle"])
        kept = roster.Character(key="a:3", name="Sampo", join_keys=["sampo"], image="source-picture-2")
        hand = manual.Manual(
            images={"Hyacine": pathlib.Path("manual/testgame/images/Hyacine.png"),
                    "Belle": pathlib.Path("manual/testgame/images/Belle.png"),
                    "Sampo": pathlib.Path("manual/testgame/images/keep/Sampo.png")},
            kept_images={"Sampo"},
        )
        result = self.run_it([with_picture, without, kept], [folder("Hyacine", "00000001"), folder("Belle", "00000002"), folder("Sampo", "00000003")], hand=hand)
        variants = self.by_name(result)
        self.assertIsNone(variants["Hyacine"].image_path)
        self.assertEqual(variants["Hyacine"].image_url, "source-picture")
        self.assertEqual(variants["Belle"].image_path, pathlib.Path("manual/testgame/images/Belle.png"), "no source picture: the hand-added one fills the gap")
        self.assertEqual(variants["Sampo"].image_path, pathlib.Path("manual/testgame/images/keep/Sampo.png"), "kept: used whatever the source has")
        self.assertTrue(any("Hyacine.png` is not used any more" in row and "images/keep/" in row for row in result.manual_rows))
        self.assertTrue(any("Sampo.png` is **Sampo**'s portrait (from `images/keep/`" in row for row in result.manual_rows))

    def test_pictures_in_keep_are_read_and_win_over_the_same_name_outside_it(self):
        with helpers.tempfile.TemporaryDirectory() as scratch:
            folder_ = helpers.pathlib.Path(scratch) / "testgame"
            (folder_ / "images" / "keep").mkdir(parents=True)
            for name in ("images/Belle.png", "images/Sampo.png", "images/keep/Sampo.webp", "images/_game.png", "images/keep/notes.txt"):
                (folder_ / name).write_bytes(b"x")
            hand = manual.read(folder_)
        self.assertEqual({k: v.relative_to(folder_).as_posix() for k, v in hand.images.items()},
                         {"Belle": "images/Belle.png", "Sampo": "images/keep/Sampo.webp"})
        self.assertEqual(hand.kept_images, {"Sampo"})
        self.assertIsNotNone(hand.game_icon)

    def test_roster_joins_folders_by_whole_name_code_then_word(self):
        result = self.run_it(
            [
                character("a:1", "Kamisato Ayaka", "Ayaka"),
                character("a:2", "Jean", "Qin"),
                character("a:3", "Shikanoin Heizou", "Heizo"),
                character("a:4", "Lan Yan"),
            ],
            [folder("KamisatoAyaka", "00000001"), folder("Jean", "00000002"), folder("Heizou", "00000003")],
        )
        variants = self.by_name(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(variants["KamisatoAyaka"].display, "Kamisato Ayaka")
        self.assertEqual(variants["Heizou"].display, "Shikanoin Heizou")
        self.assertTrue(variants["Heizou"].hashes)
        self.assertEqual(variants["LanYan"].hashes, [])  # pending, still a character
        self.assertEqual(result.ledger, {"a:1": "KamisatoAyaka", "a:2": "Jean", "a:3": "Heizou", "a:4": "LanYan"})

    def test_a_word_never_takes_a_folder_from_a_whole_name(self):
        result = self.run_it([character("a:9", "Soldier 0 - Anby"), character("a:1", "Anby")], [folder("Anby", "00000001")])
        variants = self.by_name(result)
        self.assertIsNotNone(variants["Anby"].folder)
        self.assertIsNone(variants["Soldier0Anby"].folder)

    def test_a_published_name_never_changes(self):
        result = self.run_it([character("a:4", "Lan Yan (renamed)")], [], ledger={"a:4": "LanYan"})
        self.assertEqual([v.name for v in result.variants], ["LanYan"])
        self.assertEqual(result.variants[0].display, "Lan Yan (renamed)")

    def test_a_pending_character_joins_its_folder_when_it_arrives(self):
        result = self.run_it([character("a:4", "Lan Yan")], [folder("Lanyan", "00000009")], ledger={"a:4": "LanYan"})
        self.assertEqual(result.variants[0].name, "LanYan")
        self.assertEqual(result.variants[0].hashes[0]["hash"], "00000009")

    def test_an_entry_named_like_a_character_is_part_of_its_model_and_one_naming_nobody_is_left_out(self):
        result = self.run_it(
            [character("a:1", "Augusta"), character("a:2", "Calcharo")],
            [folder("Augusta", "00000001"), folder("AugustaSword", "00000002"), folder("CalcharoBody", "00000003"), folder("Abyss_Surges", "00000004")],
        )
        variants = self.by_name(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(set(variants), {"Augusta", "Calcharo"}, "a hash entry never makes a character")
        self.assertEqual(sorted(e["hash"] for e in variants["Augusta"].hashes), ["00000001", "00000002"])
        self.assertEqual([e["hash"] for e in variants["Calcharo"].hashes], ["00000003"])
        self.assertTrue(any(i.name == "AugustaSword" and "part of Augusta's model" in i.rule for i in result.inferences))
        self.assertEqual([(i.key, i.name) for i in result.left_out], [("hashes:Abyss_Surges", "Abyss_Surges")])
        self.assertTrue(any("'Abyss_Surges'" in n and "exclude" in n for n in result.notes))

    def test_an_excluded_entry_or_one_with_no_hashes_is_left_out_without_a_note(self):
        empty = Folder("UID", "UID", [])
        result = self.run_it([character("a:1", "Augusta")], [folder("Augusta", "00000001"), folder("Motorbike", "00000002"), empty], Overrides(exclude=["Motorbike"]))
        self.assertEqual(result.errors, [])
        reasons = {i.name: i.reason for i in result.left_out}
        self.assertIn("exclude", reasons["Motorbike"])
        self.assertIn("no hashes", reasons["UID"])
        self.assertEqual(result.notes, [])

    def test_an_entry_that_may_be_a_waiting_outfit_stops_the_build_until_part_of_says(self):
        zani = character("a:1", "Zani")
        zani.outfits = [roster.Outfit("skin:1", "Downtown Repose", None)]
        unsure = self.run_it([zani], [folder("Zani", "00000001"), folder("ZaniSkin1_Interface", "00000002")])
        self.assertTrue(any("'ZaniSkin1_Interface'" in e and "Downtown Repose" in e for e in unsure.errors))
        settled = self.run_it([zani], [folder("Zani", "00000001"), folder("ZaniSkin1_Interface", "00000002")], Overrides(part_of={"ZaniSkin1_Interface": "ZaniDowntown"}))
        variants = self.by_name(settled)
        self.assertEqual(settled.errors, [])
        self.assertEqual(variants["ZaniDowntown"].parent, "Zani")
        self.assertEqual([e["hash"] for e in variants["ZaniDowntown"].hashes], ["00000002"])

    def test_an_entry_named_like_an_outfit_is_part_of_the_outfit(self):
        carlotta = character("a:1", "Carlotta")
        carlotta.outfits = [roster.Outfit("skin:1", "Splashing Summer", None)]
        result = self.run_it([carlotta], [folder("Carlotta", "00000001"), folder("CarlottaSplashingSummer", "00000002"), folder("CarlottaSplashingSummerHair", "00000003")])
        variants = self.by_name(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(sorted(e["hash"] for e in variants["CarlottaSplashingSummer"].hashes), ["00000002", "00000003"])

    def test_one_word_of_a_name_finds_a_part_only_when_no_two_characters_share_it(self):
        result = self.run_it(
            [character("a:1", "Luuk Herssen"), character("a:2", "Rover: Aero"), character("a:3", "Rover: Havoc")],
            [folder("Luuk", "00000001"), folder("LuukScalpel", "00000002"), folder("RoverMask", "00000003")],
            Overrides(join={"a:2": "RoverMale", "a:3": "RoverFemale"}),
        )
        variants = self.by_name(result)
        self.assertEqual(sorted(e["hash"] for e in variants["Luuk"].hashes), ["00000001", "00000002"])
        self.assertEqual(variants["Luuk"].display, "Luuk Herssen")
        self.assertEqual([i.name for i in result.left_out], ["RoverMask"], "'rover' is a word of two characters' names: whose is not guessed")

    def test_parents_needs_a_character_from_the_list(self):
        result = self.run_it([character("a:1", "Augusta")], [folder("Augusta", "00000001"), folder("Somebody", "00000002")], Overrides(parents={"Somebody": None}))
        self.assertTrue(any("'Somebody'" in e and "Only the character list makes a character" in e for e in result.errors))

    def test_the_same_outfit_listed_under_two_entries_of_one_character_is_added_once(self):
        forms = []
        for number in (1, 2):
            form = character(f"a:{number}", "Rover: Aero")
            form.outfits = [roster.Outfit(f"skin:{number}", "Perpetual Spark", None)]
            forms.append(form)
        result = self.run_it(forms, [folder("RoverMale", "00000001")], Overrides(join={"a:1": "RoverMale", "a:2": "RoverMale"}))
        self.assertEqual(sorted(v.name for v in result.variants), ["RoverMale", "RoverMalePerpetual"])
        self.assertEqual([(i.key, "skin:1" in i.reason) for i in result.left_out], [("skin:2", True)])

    def test_named_outfits_follow_upstream_naming_and_join_later(self):
        ganyu = character("a:1", "Ganyu")
        ganyu.outfits = [roster.Outfit("costume:GanyuCostumeYu", "Twilight Blossom", None)]
        first = self.run_it([ganyu], [folder("Ganyu", "00000001")])
        self.assertIn("GanyuTwilight", self.by_name(first))
        later = self.run_it([ganyu], [folder("Ganyu", "00000001"), folder("GanyuTwilight", "00000002")], ledger=first.ledger)
        self.assertEqual(later.errors, [])
        self.assertTrue(self.by_name(later)["GanyuTwilight"].hashes)

    def test_an_outfit_finds_its_folder_by_any_word_of_its_name(self):
        castorice = character("a:1", "Castorice")
        castorice.outfits = [roster.Outfit("skin:1140701", "Gossamer Flutter", "picture")]
        result = self.run_it([castorice], [folder("Castorice", "00000001"), folder("CastoriceFlutter", "00000002")])
        variants = self.by_name(result)
        self.assertEqual(result.errors, [])
        self.assertEqual((variants["CastoriceFlutter"].parent, variants["CastoriceFlutter"].display), ("Castorice", "Gossamer Flutter"))
        self.assertEqual(variants["CastoriceFlutter"].image_url, "picture")
        self.assertTrue(variants["CastoriceFlutter"].hashes)
        self.assertEqual(result.ledger["skin:1140701"], "CastoriceFlutter")

    def test_a_waiting_outfit_keeps_its_name_when_its_folder_arrives_under_another_word(self):
        hyacine = character("a:1", "Hyacine")
        hyacine.outfits = [roster.Outfit("skin:1140901", "Warm Cotton Skies", None)]
        first = self.run_it([hyacine], [folder("Hyacine", "00000001")])
        self.assertIn("HyacineWarm", self.by_name(first))
        later = self.run_it([hyacine], [folder("Hyacine", "00000001"), folder("HyacineCotton", "00000002")], ledger=first.ledger)
        variants = self.by_name(later)
        self.assertEqual(later.errors, [])
        self.assertNotIn("HyacineCotton", variants)  # not a second outfit: the waiting one's folder
        self.assertEqual(variants["HyacineWarm"].hashes[0]["hash"], "00000002")

    def test_a_waiting_outfit_is_named_after_a_word_that_says_something(self):
        traveler = character("a:1", "Traveler")
        traveler.outfits = [roster.Outfit("skin:200501", "As Heaven and Earth Are Made Anew", None)]
        result = self.run_it([traveler], [folder("Traveler", "00000001")])
        self.assertIn("TravelerHeaven", self.by_name(result))

    def test_a_published_outfit_keeps_its_folder_before_another_outfit_s_words_can_take_it(self):
        remielle = character("a:1", "Remielle")
        remielle.outfits = [
            roster.Outfit("skin:3115813", "Moonlight Whispers (Veil)", None),
            roster.Outfit("skin:3115811", "Moonlight Whispers", None),
        ]
        result = self.run_it([remielle], [folder("Remielle", "00000001"), folder("RemielleMoonlight", "00000002")], ledger={"a:1": "Remielle", "skin:3115811": "RemielleMoonlight"})
        variants = self.by_name(result)
        self.assertEqual(variants["RemielleMoonlight"].display, "Moonlight Whispers")
        self.assertTrue(variants["RemielleMoonlight"].hashes)
        veil = next(v for v in result.variants if v.roster_key == "skin:3115813")
        self.assertEqual((veil.name, veil.hashes), ("RemielleMoonlightWhispersVeil", []))

    def test_an_unnamed_outfit_is_reported_unless_the_ledger_already_knows_it(self):
        aria = character("a:1", "Aria")
        aria.outfits = [roster.Outfit("skin:3115011", None, "known"), roster.Outfit("skin:3115099", None, "new")]
        result = self.run_it([aria], [folder("Aria", "00000001"), folder("AriaDiscordant", "00000002")], ledger={"a:1": "Aria", "skin:3115011": "AriaDiscordant"})
        variants = self.by_name(result)
        self.assertEqual((variants["AriaDiscordant"].display, variants["AriaDiscordant"].image_url), ("Aria Discordant", "known"))
        self.assertEqual([(i.key, i.name) for i in result.left_out], [("skin:3115099", "an outfit of Aria")])
        self.assertIn("no name", result.left_out[0].reason)

    def test_a_second_entry_with_a_character_s_name_is_reported_not_added_twice(self):
        twin = character("a:1582", "Remielle")
        twin.outfits = [roster.Outfit("skin:1", "Something", None)]
        result = self.run_it([character("a:1581", "Remielle"), twin], [folder("Remielle", "00000001")])
        self.assertEqual([v.name for v in result.variants], ["Remielle"])
        self.assertEqual([i.key for i in result.left_out], ["a:1582"])
        self.assertIn("a:1581", result.left_out[0].reason)
        self.assertIn("1 outfit", result.left_out[0].name)
        named = self.run_it([character("a:1581", "Remielle"), twin], [folder("Remielle", "00000001")], Overrides(join={"a:1582": "RemielleEvent"}))
        self.assertIn("RemielleEvent", self.by_name(named))
        self.assertEqual(named.left_out, [])

    def test_a_new_form_of_a_character_joins_the_name_its_other_forms_have(self):
        forms = []
        for element in ("pyro", "hydro"):
            form = character(f"avatar:10000117-{element}", "Manekin", **{"element": "Ice"})
            form.family = "avatar:10000117"
            forms.append(form)
        result = self.run_it(forms, [folder("Manekin", "00000001")], ledger={"avatar:10000117": "Manekin"})
        self.assertEqual([v.name for v in result.variants], ["Manekin"])
        self.assertEqual(result.ledger["avatar:10000117-hydro"], "Manekin")

    def test_a_replacement_in_manual_stands_in_for_upstream_s_hashes(self):
        hand = manual.Manual(replacements={"Ganyu": [{"variant": "", "component": "", "kind": "ib", "hash": "0000beef"}]},
                             replacement_sources={"Ganyu": "manual/testgame/hashes/replace/Ganyu.txt"})
        result = self.run_it([character("a:1", "Ganyu")], [folder("Ganyu", "00000001")], hand=hand)
        ganyu = self.by_name(result)["Ganyu"]
        self.assertEqual([e["hash"] for e in ganyu.hashes], ["0000beef"])
        self.assertEqual(result.replaced, ["Ganyu"])
        self.assertTrue(any("replaces upstream's" in row for row in result.manual_rows))

    def test_a_replacement_for_nobody_stops_the_build(self):
        hand = manual.Manual(replacements={"Nobody": [{"variant": "", "component": "", "kind": "ib", "hash": "0000beef"}]})
        result = self.run_it([character("a:1", "Ganyu")], [folder("Ganyu", "00000001")], hand=hand)
        self.assertTrue(any("nothing for it to replace" in e for e in result.errors))

    def test_replacement_files_are_read_from_their_own_folder(self):
        with helpers.tempfile.TemporaryDirectory() as scratch:
            folder_ = helpers.pathlib.Path(scratch) / "testgame"
            (folder_ / "hashes" / "replace").mkdir(parents=True)
            (folder_ / "hashes" / "Ganyu.txt").write_text("ib 1575ec63\n")
            (folder_ / "hashes" / "replace" / "Keqing.txt").write_text("ib 0000beef\n")
            (folder_ / "hashes" / "replace" / "Empty.txt").write_text("nothing here\n")
            hand = manual.read(folder_)
        self.assertEqual(list(hand.hashes), ["Ganyu"])
        self.assertEqual(list(hand.replacements), ["Keqing"])
        self.assertEqual(hand.replacement_sources["Keqing"], "manual/testgame/hashes/replace/Keqing.txt")
        self.assertTrue(any("replace/Empty.txt: no hashes" in p for p in hand.problems))

    def test_several_roster_entries_for_one_character_merge(self):
        overrides = Overrides(join={"a:1": "TravelerBoy", "a:2": "TravelerBoy"}, display_names={"TravelerBoy": "Aether"})
        one = character("a:1", "Traveler", element="Ice")
        two = character("a:2", "Traveler", element="Nope")
        result = self.run_it([one, two], [folder("TravelerBoy", "00000001")], overrides)
        self.assertEqual([v.name for v in result.variants], ["TravelerBoy"])
        self.assertEqual(result.variants[0].attributes["element"], ["cryo", "nope"])
        self.assertTrue(any("New element value 'Nope'" in n for n in result.notes))

    def test_manual_hashes_are_kept_beside_upstream_and_can_create_a_character(self):
        hand = manual.Manual(
            hashes={
                "ganyu": [{"variant": "", "component": "", "kind": "ib", "hash": "00000001"}, {"variant": "", "component": "", "kind": "unknown", "hash": "0000beef"}],
                "Brand New": [{"variant": "", "component": "", "kind": "unknown", "hash": "12345678"}],
            },
            hash_sources={"ganyu": "manual/x/hashes/ganyu.txt", "Brand New": "manual/x/hashes/Brand New.txt"},
        )
        result = self.run_it([character("a:1", "Ganyu")], [folder("Ganyu", "00000001")], hand=hand)
        variants = self.by_name(result)
        self.assertEqual(sorted(e["hash"] for e in variants["Ganyu"].hashes if e["kind"] != "root_vs"), ["00000001", "0000beef"])
        self.assertEqual(variants["BrandNew"].display, "Brand New")
        rows = "\n".join(result.manual_rows)
        self.assertIn("now in upstream too", rows)
        self.assertIn("**BrandNew** was added as a new one", rows)

    def test_part_of_folds_a_form_into_its_character(self):
        result = self.run_it(
            [character("a:1", "Firefly")],
            [folder("Firefly", "00000001"), folder("SAM", "00000003")],
            overrides=Overrides(part_of={"SAM": "Firefly"}),
        )
        variants = self.by_name(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(set(variants), {"Firefly"})
        self.assertIn("00000003", [e["hash"] for e in variants["Firefly"].hashes])

    def test_part_of_a_character_that_does_not_exist_stops_the_build(self):
        result = self.run_it(
            [character("a:1", "Firefly")],
            [folder("Firefly", "00000001"), folder("SAM", "00000003")],
            overrides=Overrides(part_of={"SAM": "Nobody"}),
        )
        self.assertTrue(any("partOf" in e and "Nobody" in e for e in result.errors))

    def test_an_outfit_parents_names_takes_its_characters_display_name(self):
        result = self.run_it(
            [character("a:1", "March 7th (Preservation)", "March7thPreservation"), character("a:2", "Caelus")],
            [folder("March7thPreservation", "00000001"), folder("March7thPreservationSpring", "00000002"), folder("CaelusVigor", "00000003"),
             folder("OddName", "00000004")],
            overrides=Overrides(parents={"March7thPreservationSpring": "March7thPreservation", "CaelusVigor": "Caelus", "OddName": "Caelus"}),
        )
        variants = self.by_name(result)
        self.assertEqual(variants["March7thPreservationSpring"].display, "March 7th (Preservation) Spring")
        self.assertEqual(variants["CaelusVigor"].display, "Caelus Vigor")
        self.assertEqual(variants["OddName"].display, "Odd Name")  # not named like its character: the folder's words

    def test_a_manual_picture_for_nobody_stops_the_build(self):
        hand = manual.Manual(images={"Nobody": helpers.pathlib.Path("manual/x/images/Nobody.png")})
        result = self.run_it([character("a:1", "Ganyu")], [], hand=hand)
        self.assertTrue(any("Nobody.png" in e for e in result.errors))


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.fake = helpers.FakeRepo()

    def tearDown(self):
        self.fake.cleanup()

    def build(self, **kwargs):
        return build_game(self.fake.repo, "testgame", fetcher=kwargs.pop("fetcher", None), today=kwargs.pop("today", DAY), **kwargs)

    def test_a_first_build_writes_a_complete_pack(self):
        result = self.build()
        self.assertEqual(result.errors, [])
        self.assertTrue(result.changed)
        self.assertEqual(result.version, "2026.09.25")
        variants = {v["internalName"]: v for v in self.fake.read("packs/testgame/variants.json")}
        self.assertEqual(set(variants), {"Ganyu", "GanyuTwilight", "LanYan"})
        self.assertTrue(variants["LanYan"]["hashesPending"])
        self.assertEqual(variants["Ganyu"]["attributes"], {"element": "cryo", "rarity": 5})
        self.assertEqual(self.fake.read("packs/testgame/hashes.json")["ignoredHashes"], [])
        main = [e for e in self.fake.read("packs/testgame/hashes.json")["entries"] if e["variant"] == "Ganyu"]
        self.assertEqual([(e["kind"], e["hash"]) for e in main], [("ib", "1575ec63"), ("texture", "6d78ac96")])
        manifest = self.fake.read("packs/testgame/manifest.json")
        self.assertEqual(manifest["counts"], {"variants": 3, "skins": 1, "images": 0})
        self.assertEqual(manifest["authoredBy"], "official")
        for report in ("pending-hashes", "missing-images", "inference-report", "collisions", "manual"):
            self.assertTrue((self.fake.root / "reports" / "testgame" / f"{report}.md").is_file(), report)

    def test_a_surprise_in_one_game_is_that_game_s_error_with_its_traceback(self):
        # Audit P3: an AttributeError from an odd upstream answer used to stop every game's build.
        with mock.patch.object(build.assembler, "assemble", side_effect=AttributeError("'list' object has no attribute 'get'")):
            result = self.build()
        self.assertTrue(result.errors)
        self.assertIn("AttributeError", result.errors[0])
        blocked = (self.fake.root / "reports" / "testgame" / "blocked.md").read_text()
        self.assertIn("Traceback", blocked)
        self.assertFalse((self.fake.root / "packs" / ".testgame.new").exists(), "no half-built pack is left (P8)")

    def test_nothing_changed_means_the_same_version_and_bytes(self):
        self.build()
        before = (self.fake.root / "packs/testgame/manifest.json").read_bytes()
        again = self.build(today=DAY + datetime.timedelta(days=7))
        self.assertFalse(again.changed)
        self.assertEqual(again.version, "2026.09.25")
        self.assertEqual(before, (self.fake.root / "packs/testgame/manifest.json").read_bytes())

    def test_a_manual_file_makes_a_new_version(self):
        self.build()
        self.fake.write("manual/testgame/hashes/LanYan.txt", "[TextureOverrideLanYanIB]\nhash = 7a7a7a7a\n")
        self.fake.write("manual/testgame/images/LanYan.png", helpers.png())
        result = self.build(today=DAY + datetime.timedelta(days=1))
        self.assertTrue(result.changed)
        self.assertEqual(result.version, "2026.09.26")
        self.assertIn("Hashes for the first time: Lan Yan.", result.summary)
        self.assertIn("New portraits: Lan Yan.", result.summary)
        variants = {v["internalName"]: v for v in self.fake.read("packs/testgame/variants.json")}
        self.assertNotIn("hashesPending", variants["LanYan"])
        self.assertEqual(variants["LanYan"]["image"], "images/LanYan.webp")
        self.assertTrue((self.fake.root / "packs/testgame/images/LanYan.webp").is_file())

    def test_a_game_icon_in_manual_becomes_the_packs_icon_and_is_not_a_character(self):
        self.build()
        self.fake.write("manual/testgame/images/_game.png", helpers.png(size=(90, 60)))
        result = self.build(today=DAY + datetime.timedelta(days=1))
        self.assertEqual(result.errors, [])
        self.assertTrue(result.changed)
        self.assertEqual(self.fake.read("packs/testgame/game.json")["icon"], "images/_game.webp")
        variants = {v["internalName"] for v in self.fake.read("packs/testgame/variants.json")}
        self.assertEqual(variants, {"Ganyu", "GanyuTwilight", "LanYan"})
        with helpers.Image.open(self.fake.root / "packs/testgame/images/_game.webp") as icon:
            self.assertEqual(icon.width, icon.height)  # padded to a square, never cut
            self.assertEqual(icon.getpixel((0, 0))[3], 0)
        self.assertIn("game icon", (self.fake.root / "packs/testgame/ATTRIBUTION.md").read_text())
        self.assertIn("is the game's icon", (self.fake.root / "reports/testgame/manual.md").read_text())

        again = self.build(today=DAY + datetime.timedelta(days=2))
        self.assertFalse(again.changed)  # the same icon is the same pack

    def test_removing_the_game_icon_removes_it_from_the_pack(self):
        self.fake.write("manual/testgame/images/_game.png", helpers.png())
        self.build()
        (self.fake.root / "manual/testgame/images/_game.png").unlink()
        result = self.build(today=DAY + datetime.timedelta(days=1))
        self.assertTrue(result.changed)
        self.assertNotIn("icon", self.fake.read("packs/testgame/game.json"))
        self.assertFalse((self.fake.root / "packs/testgame/images/_game.webp").exists())

    def test_a_game_icon_that_is_not_a_picture_stops_the_build_and_says_so(self):
        self.fake.write("manual/testgame/images/_game.png", "not a picture")
        result = self.build()
        self.assertTrue(any("_game.png" in e and "not a picture" in e for e in result.errors))
        self.assertIn("_game.png", (self.fake.root / "reports/testgame/blocked.md").read_text())

    def with_store_icon(self):
        config = self.fake.read("config/testgame.json")
        self.fake.write("config/testgame.json", {**config, "icon": {"googlePlay": "com.example.game"}})

    def build_online(self, store, **kwargs):
        return self.build(fetcher=store, refresh_roster=False, refresh_hashes=False, **kwargs)

    def test_the_game_icon_comes_from_the_store_and_is_downloaded_again_only_when_it_changes(self):
        self.with_store_icon()
        store = FakeStore("https://play-lh.googleusercontent.com/first")
        result = self.build_online(store)
        self.assertEqual(result.errors, [])
        self.assertEqual(self.fake.read("packs/testgame/game.json")["icon"], "images/_game.webp")
        self.assertEqual(self.fake.read("upstream/testgame/icon.json")["source"], "https://play-lh.googleusercontent.com/first=s512")
        self.assertIn("Google Play", (self.fake.root / "packs/testgame/ATTRIBUTION.md").read_text())
        self.assertIn("id=com.example.game&hl=en&gl=US", store.pages[0])

        again = self.build_online(store, today=DAY + datetime.timedelta(days=7))
        self.assertFalse(again.changed)
        self.assertEqual(store.pictures, ["https://play-lh.googleusercontent.com/first=s512"])  # not downloaded twice

        store.icon = "https://play-lh.googleusercontent.com/anniversary"
        store.colour = (10, 200, 10, 255)
        changed = self.build_online(store, today=DAY + datetime.timedelta(days=14))
        self.assertTrue(changed.changed)
        self.assertEqual(len(store.pictures), 2)

    def test_a_store_that_cannot_be_reached_keeps_last_weeks_icon(self):
        self.with_store_icon()
        self.build_online(FakeStore("https://play-lh.googleusercontent.com/first"))
        before = (self.fake.root / "packs/testgame/images/_game.webp").read_bytes()

        result = self.build_online(FakeStore(None), today=DAY + datetime.timedelta(days=7))
        self.assertEqual(result.errors, [])
        self.assertFalse(result.changed)
        self.assertTrue(any("Game icon not refreshed" in w for w in result.warnings))
        self.assertEqual(before, (self.fake.root / "packs/testgame/images/_game.webp").read_bytes())

    def test_a_game_icon_in_manual_beats_the_stores(self):
        self.with_store_icon()
        self.fake.write("manual/testgame/images/_game.png", helpers.png())
        store = FakeStore("https://play-lh.googleusercontent.com/first")
        self.build_online(store)
        self.assertEqual(store.pages, [])
        self.assertIn("kept by hand", (self.fake.root / "packs/testgame/ATTRIBUTION.md").read_text())

    def test_a_new_upstream_character_is_published_and_named_in_the_changelog(self):
        # MILESTONES.md M13's exit: a scheduled run with a new upstream character publishes it.
        self.build()
        roster = self.fake.read("upstream/testgame/roster.json")["characters"]
        self.fake.set_roster(roster + [{"key": "avatar:3", "name": "Nefer", "joinKeys": ["nefer"]}])
        result = self.build(today=DAY + datetime.timedelta(days=7))
        self.assertTrue(result.changed)
        self.assertEqual(result.version, "2026.10.02")
        self.assertIn("Added: Nefer.", result.summary)
        variants = {v["internalName"]: v for v in self.fake.read("packs/testgame/variants.json")}
        self.assertTrue(variants["Nefer"]["hashesPending"])
        self.assertIn("**Nefer**", (self.fake.root / "reports/testgame/pending-hashes.md").read_text())

    def test_the_notes_inside_manual_are_not_data(self):
        self.fake.write("manual/testgame/README.md", "# notes\nib 12345678\n")
        self.fake.write("manual/testgame/hashes/README.md", "# notes\nib 12345678\n")
        self.fake.write("manual/testgame/images/README.md", "# notes\n")
        self.fake.write("manual/testgame/hashes/.gitkeep", "")
        self.fake.write("manual/testgame/images/.gitkeep", "")
        result = self.build()
        self.assertEqual(result.errors, [])
        names = {v["internalName"] for v in self.fake.read("packs/testgame/variants.json")}
        self.assertEqual(names, {"Ganyu", "GanyuTwilight", "LanYan"})

    def test_a_second_release_the_same_day_gets_a_suffix(self):
        self.build()
        self.fake.write("manual/testgame/hashes/LanYan.txt", "ib 7a7a7a7a\n")
        self.assertEqual(self.build(known_versions={"2026.09.25"}).version, "2026.09.25.01")

    def test_a_disappearing_character_stops_the_build_and_changes_nothing(self):
        self.build()
        before = {p.name: p.read_bytes() for p in (self.fake.root / "packs/testgame").glob("*.json")}
        self.fake.set_roster([{"key": "avatar:1", "name": "Ganyu", "joinKeys": ["ganyu"], "outfits": [{"key": "skin:11", "name": "Twilight Blossom"}]}])
        result = self.build(today=DAY + datetime.timedelta(days=7))
        self.assertTrue(any("LanYan" in e for e in result.errors))
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.fake.root / "packs/testgame").glob("*.json")})
        self.assertFalse((self.fake.root / "packs/.testgame.new").exists())
        self.assertIn("LanYan", (self.fake.root / "reports/testgame/blocked.md").read_text())

    def test_a_misspelt_override_is_an_error_not_ignored(self):
        self.fake.write("overrides/testgame.json", {"parent": {"GanyuTwilight": None}})
        result = self.build()
        self.assertTrue(any("unknown key(s) parent" in e for e in result.errors))

    def test_outfit_images_in_overrides_is_an_error_that_says_what_replaced_it(self):
        self.fake.write("overrides/testgame.json", {"outfitImages": {"GanyuTwilight": "skin:1"}})
        result = self.build()
        self.assertTrue(any("\"outfitImages\" is no longer used" in e and "\"join\"" in e for e in result.errors))

    def test_no_release_dates_are_published(self):
        self.fake.set_roster([{"key": "avatar:1", "name": "Ganyu", "joinKeys": ["ganyu"], "releaseDate": "2021-01-12"}])
        self.assertEqual(self.build().errors, [])
        self.assertFalse([v for v in self.fake.read("packs/testgame/variants.json") if "releaseDate" in v])

    def test_what_the_character_list_left_out_is_in_its_own_report(self):
        self.fake.write(f"upstream/testgame/roster.json", {
            "source": "nanoka",
            "characters": [{"key": "avatar:1", "name": "Ganyu", "joinKeys": ["ganyu"]}, {"key": "avatar:3", "name": "Ganyu", "joinKeys": ["ganyu"]}],
            "leftOut": [{"key": "avatar:7005", "name": "Kafka", "reason": "its number is outside the playable range"}],
        })
        self.assertEqual(self.build().errors, [])
        report = (self.fake.root / "reports" / "testgame" / "left-out.md").read_text()
        self.assertIn("2 entries", report)
        self.assertIn("`avatar:7005` **Kafka** — its number is outside the playable range.", report)
        self.assertIn("`avatar:3` **Ganyu**", report)

    def test_a_replacement_smaller_than_upstream_s_hashes_is_not_stopped_as_a_shrink(self):
        self.fake.set_folders({"Ganyu": helpers.component("00000000", old=[f"000000{i:02d}" for i in range(1, 10)])})
        self.assertEqual(self.build().errors, [])
        self.fake.write("manual/testgame/hashes/replace/Ganyu.txt", "ib 0000beef\n")
        result = self.build(today=DAY + datetime.timedelta(days=1))
        self.assertEqual(result.errors, [])
        ganyu = [e["hash"] for e in self.fake.read("packs/testgame/hashes.json")["entries"] if e["variant"] == "Ganyu"]
        self.assertEqual(ganyu, ["0000beef"])

    def test_a_value_in_the_hash_source_that_is_not_a_hash_is_left_out_and_said(self):
        entry = helpers.component("1575ec63")
        entry["main_hashes"][0]["old"] = ["7d1be58"]
        self.fake.set_folders({"Ganyu": entry})
        result = self.build()
        self.assertEqual(result.errors, [])
        self.assertTrue(any("'Ganyu'" in w and "'7d1be58'" in w and "8 or 16 hex digits" in w for w in result.warnings))
        self.assertNotIn("7d1be58", [e["hash"] for e in self.fake.read("packs/testgame/hashes.json")["entries"]])

    def test_no_saved_sources_and_no_network_is_a_clear_error(self):
        (self.fake.root / "upstream/testgame/roster.json").unlink()
        result = self.build()
        self.assertTrue(any("no character list" in e for e in result.errors))


class LearnTest(unittest.TestCase):
    def setUp(self):
        self.fake = helpers.FakeRepo()
        build_game(self.fake.repo, "testgame", fetcher=None, today=DAY)

    def tearDown(self):
        self.fake.cleanup()

    def test_learning_keeps_only_the_characters_own_hashes(self):
        mod = self.fake.root / "mods" / "LanYanMod"
        self.fake.write(
            "mods/LanYanMod/LanYan.ini",
            "[TextureOverrideLanYanBodyIB]\nhash = 7a7a7a7a\n"
            "[TextureOverrideLanYanPosition]\nhash = 7b7b7b7b\n"
            "[TextureOverrideGanyuThingIB]\nhash = 1575ec63\n"
            "[ShaderOverrideX]\nhash = 653c63ba4a73ca8b\n"
            "[TextureOverrideShaderish]\nhash = 0123456789abcdef\n",
        )
        learned = learn.learn(self.fake.repo.manual("testgame"), self.fake.repo.pack("testgame"), "lanyan", mod)
        self.assertEqual([(e["kind"], e["hash"]) for e in learned.added], [("ib", "7a7a7a7a"), ("position_vb", "7b7b7b7b")])
        self.assertEqual(learned.elsewhere, {"1575ec63": ["Ganyu"]})
        self.assertEqual(learned.shaders, 1)
        text = (self.fake.root / "manual/testgame/hashes/LanYan.txt").read_text()
        self.assertIn("# from LanYanMod", text)

        again = learn.learn(self.fake.repo.manual("testgame"), self.fake.repo.pack("testgame"), "LanYan", mod)
        self.assertEqual(again.added, [])
        self.assertEqual(again.already, 2)

    def test_a_folder_with_no_ini_is_refused(self):
        (self.fake.root / "empty").mkdir()
        with self.assertRaises(Exception) as caught:
            learn.learn(self.fake.repo.manual("testgame"), self.fake.repo.pack("testgame"), "LanYan", self.fake.root / "empty")
        self.assertIn("No .ini files", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class FakeStore:
    """Google Play, as far as the icon needs it: a page naming the icon, and the icon. None is a store that is down."""

    def __init__(self, icon):
        self.icon = icon
        self.colour = (200, 100, 50, 255)
        self.pages: list[str] = []
        self.pictures: list[str] = []

    def get(self, url, *, fresh=True):
        from packbuilder.http import FetchError

        if self.icon is None:
            raise FetchError(f"{url}: HTTP 503 Service Unavailable")
        if url.startswith("https://play.google.com/"):
            self.pages.append(url)
            return f'<html><meta property="og:image" content="{self.icon}=s0-br30"></html>'.encode()
        self.pictures.append(url)
        return helpers.png(self.colour, (40, 40))


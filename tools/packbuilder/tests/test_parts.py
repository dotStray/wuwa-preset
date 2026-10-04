"""The small pieces: names, the hash source, pasted hashes, checks, versions, zips, pictures."""

from __future__ import annotations

import datetime
import json
import pathlib
import unittest
import zlib
from unittest import mock

import helpers  # noqa: F401  (puts src on the path)

from packbuilder import assemble, build, checks, hashes, images, manual, names, release


class NamesTest(unittest.TestCase):
    def test_pascal_keeps_capitals_and_drops_punctuation(self):
        self.assertEqual(names.pascal("Lan Yan"), "LanYan")
        self.assertEqual(names.pascal("Soldier 0 - Anby"), "Soldier0Anby")
        self.assertEqual(names.pascal("Orchid's Evening Gown"), "OrchidsEveningGown")
        self.assertEqual(names.pascal("Kamisato Ayaka"), "KamisatoAyaka")

    def test_split_camel(self):
        self.assertEqual(names.split_camel("GanyuTwilight"), "Ganyu Twilight")
        self.assertEqual(names.split_camel("SilverWolf999"), "Silver Wolf 999")
        self.assertEqual(names.split_camel("DanHengIL"), "Dan Heng IL")

    def test_markup_is_removed_from_names(self):
        self.assertEqual(names.clean_name("Silver Wolf LV.<unbreak>999</unbreak>"), "Silver Wolf LV.999")

    def test_join_key_ignores_case_accents_and_punctuation(self):
        self.assertEqual(names.join_key("Dr. Ratio"), "drratio")
        self.assertEqual(names.join_key("Kirara"), names.join_key("KIRARA"))
        self.assertEqual(names.join_key("Chénzhōu"), "chenzhou")


class UpstreamHashesTest(unittest.TestCase):
    def test_empty_strings_mean_absent(self):
        found = hashes.entries("Ganyu", [{"component_name": "Face", "root_vs": "", "ib": "", "draw_vb": "ABCDEF01", "texture_hashes": [[]]}])
        self.assertEqual(found, [{"variant": "Ganyu", "component": "Face", "kind": "draw_vb", "hash": "abcdef01"}])

    def test_textures_keep_their_kind_and_slot_and_skip_malformed(self):
        found = hashes.entries(
            "X",
            [{"ib": "11111111", "texture_hashes": [[["Diffuse", ".dds", "22222222"], ["bad"]], [], [["", ".dds", "33333333"]]]}],
        )
        textures = [e for e in found if e["kind"] == "texture"]
        self.assertEqual([(t["hash"], t["textureKind"], t["slot"]) for t in textures], [("22222222", "Diffuse", 0), ("33333333", "Unknown", 2)])

    def test_not_hex_is_not_a_hash_and_a_hash_is_8_or_16_digits(self):
        self.assertEqual(hashes.entries("X", [{"ib": "zzzz0000"}]), [])
        self.assertEqual(hashes.entries("X", [{"ib": "7d1be58"}, {"ib": "1234567890"}]), [])

    def test_duplicates_dropped(self):
        found = hashes.entries("X", [{"ib": "11111111"}, {"ib": "11111111"}])
        self.assertEqual(len(found), 1)


class MoonholderTest(unittest.TestCase):
    ENTRY = {
        "main_hashes": [{"old": ["aaaa0001", "aaaa0002"], "new": "AAAA0003"}, {"old": [], "new": "bbbb0001"}],
        "textures": {
            "cccc0001": {"meta": {"id": 1, "type": "D"}, "replace": ["cccc0002"], "derive": {"LOD": ["cccc0003"]}},
            "dddd0001": {"meta": {"id": "0", "type": "M"}},
            "eeee0001": {"replace": ["eeee0002"]},
        },
        "rules": [{"line_prefix": "match_first_index", "replacements": [{"old": ["129168"], "new": "130992"}]}],
        "vg_remaps": [{"trigger_hash": ["ffff0001"], "vertex_groups": "1:2"}],
    }

    def test_every_main_hash_old_and_new_is_an_ib_and_every_texture_keeps_its_kind_and_slot(self):
        found, bad = hashes.components(self.ENTRY)
        entries = hashes.entries("X", found)
        self.assertEqual(bad, [])
        self.assertEqual([e["hash"] for e in entries if e["kind"] == "ib"], ["aaaa0003", "aaaa0001", "aaaa0002", "bbbb0001"])
        textures = [(e["hash"], e["textureKind"], e["slot"]) for e in entries if e["kind"] == "texture"]
        self.assertEqual(
            sorted(textures),
            [("cccc0001", "Diffuse", 1), ("cccc0002", "Diffuse", 1), ("cccc0003", "Diffuse", 1), ("dddd0001", "MaterialMap", 0), ("eeee0001", "Unknown", 0), ("eeee0002", "Unknown", 0)],
        )
        self.assertNotIn("ffff0001", [e["hash"] for e in entries], "how the fixer rewrites a mod is not who a mod is for")

    def test_a_value_that_is_not_a_hash_is_given_back_to_be_reported(self):
        found, bad = hashes.components({"main_hashes": [{"old": ["7d1be58"], "new": "aaaa0001"}], "textures": {"c62d648": {}}})
        self.assertEqual([e["hash"] for e in hashes.entries("X", found)], ["aaaa0001"])
        self.assertEqual(bad, ["7d1be58", "c62d648"])

    def test_the_saved_copy_is_one_folder_per_entry(self):
        fake = helpers.FakeRepo()
        try:
            fake.set_folders({"Augusta": helpers.component("11111111"), "Motorbike": "not an entry", "UID": {"textures": {}}})
            folders, lock = hashes.load(fake.root / "upstream" / "testgame" / "hashes")
        finally:
            fake.cleanup()
        self.assertEqual([(f.name, len(f.components)) for f in folders], [("Augusta", 1), ("UID", 0)])
        self.assertEqual(lock["commit"], "c0ffee")


class MoonholderDownloadTest(unittest.TestCase):
    REPO = "someone/Fixer"

    class Fake:
        def __init__(self, body: bytes, sha: str):
            self.body, self.sha, self.asked = body, sha, []

        def get_json(self, url, *, fresh=True):
            self.asked.append(url)
            if url.endswith("/commits/HEAD"):
                return {"sha": "c0ffee"}
            return {"tree": [{"type": "blob", "path": "config.json", "sha": self.sha}, {"type": "blob", "path": "config.yml", "sha": "1" * 40}]}

        def get(self, url, *, fresh=True):
            self.asked.append(url)
            return self.body

    def fake(self, payload) -> "MoonholderDownloadTest.Fake":
        body = json.dumps(payload).encode()
        return self.Fake(body, hashes._blob_sha(body))

    def test_the_file_is_downloaded_only_when_its_blob_changed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as scratch:
            destination = pathlib.Path(scratch) / "hashes"
            first = self.fake({"characters": {"Augusta": helpers.component("11111111")}})
            self.assertEqual(hashes.refresh(self.REPO, "config.json", destination, first), "c0ffee")
            again = self.fake({"characters": {"Augusta": helpers.component("11111111")}})
            hashes.refresh(self.REPO, "config.json", destination, again)
            self.assertFalse([u for u in again.asked if "raw.githubusercontent.com" in u])
            self.assertEqual([f.name for f in hashes.load(destination)[0]], ["Augusta"])

    def test_a_file_that_is_broken_or_lost_its_characters_leaves_the_copy_as_it_was(self):
        import tempfile

        with tempfile.TemporaryDirectory() as scratch:
            destination = pathlib.Path(scratch) / "hashes"
            hashes.refresh(self.REPO, "config.json", destination, self.fake({"characters": {"Augusta": helpers.component("11111111")}}))
            for broken in ({"characters": {}}, {"version": "3.7"}, ["not", "an", "object"]):
                with self.assertRaises(hashes.FetchError):
                    hashes.refresh(self.REPO, "config.json", destination, self.fake(broken))
            wrong = self.fake({"characters": {"X": {}}})
            wrong.sha = "2" * 40
            with self.assertRaises(hashes.FetchError):
                hashes.refresh(self.REPO, "config.json", destination, wrong)
            self.assertEqual([f.name for f in hashes.load(destination)[0]], ["Augusta"])


class PastedHashesTest(unittest.TestCase):
    def test_ini_sections_decide_the_kind(self):
        text = """
        [TextureOverrideGanyuBodyIB]
        hash = 1575ec63
        [TextureOverrideGanyuPosition]
        hash = a5169f1d
        [TextureOverrideGanyuHeadDiffuse]
        hash = 6d78ac96
        [TextureOverrideGanyuLightMap]
        hash = 9b0d2126
        [ShaderOverrideGanyu]
        hash = 653c63ba4a73ca8b
        """
        kinds = {e["hash"]: e["kind"] for e in manual.parse_text(text)}
        self.assertEqual(kinds, {"1575ec63": "ib", "a5169f1d": "position_vb", "6d78ac96": "texture", "9b0d2126": "texture", "653c63ba4a73ca8b": "root_vs"})

    def test_a_wwmi_component_section_holds_the_model_s_main_hash(self):
        text = "[TextureOverrideComponent0]\nhash = c7aae00c\nmatch_first_index = 0\n[TextureOverrideTexture3]\nhash = 2cfea8fa\n"
        self.assertEqual({e["hash"]: e["kind"] for e in manual.parse_text(text)}, {"c7aae00c": "ib", "2cfea8fa": "texture"})

    def test_a_character_named_like_a_marker_is_not_one(self):
        self.assertIsNone(manual.section_kind("TextureOverrideZibai"))
        self.assertEqual(manual.section_kind("TextureOverrideNikeIB2"), "ib")

    def test_plain_lines(self):
        found = manual.parse_text("ib 1575ec63\n  2b3c4d5e   # a comment deadbeef\nnot a hash: deadbeef\n")
        self.assertEqual([(e["kind"], e["hash"]) for e in found], [("ib", "1575ec63"), ("unknown", "2b3c4d5e")])

    def test_texture_override_only_skips_other_sections(self):
        text = "[ResourceGanyu]\nhash = 11111111\n[TextureOverrideGanyuIB]\nhash = 22222222\n"
        self.assertEqual([e["hash"] for e in manual.parse_text(text, texture_override_only=True)], ["22222222"])


class ValidateTest(unittest.TestCase):
    game = {"attributes": {"element": {"displayName": "E", "values": [{"id": "cryo", "displayName": "Cryo"}]}}}

    def variant(self, name, parent=None, **extra):
        return {"internalName": name, "displayName": name, "baseCharacterId": parent, "isDefaultVariant": parent is None, **extra}

    def test_a_good_pack_passes(self):
        variants = [self.variant("Ganyu", attributes={"element": "cryo"}), self.variant("GanyuTwilight", "Ganyu")]
        entries = {"entries": [{"variant": "Ganyu", "kind": "ib", "hash": "1575ec63"}]}
        self.assertEqual(checks.validate(self.game, variants, entries, {}), [])

    def test_every_rule(self):
        variants = [
            self.variant("Ganyu"),
            self.variant("ganyu"),
            self.variant("Bad Name"),
            self.variant("Orphan", "Nobody"),
            self.variant("Twin", "Ganyu", isDefaultVariant=True),
            self.variant("Deep", "Twin"),
            self.variant("Odd", attributes={"element": "pyro", "weapon": "bow"}),
            self.variant("Pic", image="images/Pic.webp"),
            self.variant("Pending", hashesPending=True),
        ]
        entries = {
            "entries": [
                {"variant": "Nobody", "kind": "ib", "hash": "1575ec63"},
                {"variant": "Ganyu", "kind": "weird", "hash": "1575EC63"},
                {"variant": "Pending", "kind": "ib", "hash": "1575ec63"},
            ]
        }
        errors = "\n".join(checks.validate(self.game, variants, entries, {"images/Pic.webp": 300 * 1024}))
        for expected in [
            "same name ignoring capitals",
            "'Bad Name' is not a valid internal name",
            "'Orphan' is an outfit of 'Nobody'",
            "The family of 'ganyu' has 3 default variants",
            "The family of 'Twin' has 0 default variants",
            "which is itself an outfit",
            "element 'pyro'",
            "attribute 'weapon'",
            "300 KB",
            "for 'Nobody'",
            "kind 'weird'",
            "'1575EC63'",
            "'Pending' is marked hashesPending but has hashes",
        ]:
            self.assertIn(expected, errors)


class GuardTest(unittest.TestCase):
    before_variants = [{"internalName": "Ganyu"}, {"internalName": "Amber"}]
    before_hashes = {"entries": [{"variant": "Ganyu", "hash": f"{i:08x}"} for i in range(10)]}

    def test_a_character_disappearing_stops_the_build(self):
        problems = checks.guard(self.before_variants, self.before_hashes, [{"internalName": "Ganyu"}], self.before_hashes, [], [])
        self.assertTrue(any("Amber" in p and "retired" in p for p in problems))

    def test_retired_characters_may_go(self):
        self.assertEqual(checks.guard(self.before_variants, self.before_hashes, [{"internalName": "Ganyu"}], self.before_hashes, ["amber"], []), [])

    def test_losing_most_hashes_stops_the_build_unless_allowed(self):
        after = {"entries": self.before_hashes["entries"][:3]}
        variants = self.before_variants
        self.assertTrue(any("Ganyu (10 → 3)" in p for p in checks.guard(variants, self.before_hashes, variants, after, [], [])))
        self.assertEqual(checks.guard(variants, self.before_hashes, variants, after, [], ["Ganyu"]), [])

    def test_the_first_build_has_nothing_to_compare_with(self):
        self.assertEqual(checks.guard([], {}, [], {}, [], []), [])


class VersionAndZipTest(unittest.TestCase):
    def test_versions_sort_as_text_and_same_day_gets_a_suffix(self):
        day = datetime.date(2026, 9, 25)
        self.assertEqual(build.next_version(day, set()), "2026.09.25")
        self.assertEqual(build.next_version(day, {"2026.09.25"}), "2026.09.25.01")
        self.assertEqual(build.next_version(day, {"2026.09.25", "2026.09.25.01"}), "2026.09.25.02")
        # Audit P7: a tag that is not a date sorts after every date as text, and stopped every run.
        self.assertEqual(build.next_version(day, {"v1", "2026.09.24"}), "2026.09.25")
        self.assertGreater("2026.09.25.01", "2026.09.25")
        self.assertGreater("2026.09.25.10", "2026.09.25.09")

    def test_a_new_version_sorts_after_the_previous_one_even_when_the_releases_are_gone(self):
        # 2026-09-27: every release was deleted, so only the previous pack's 2026.09.26.01 was known,
        # and a free 2026.09.26 was published — which the app sorts as older.
        day = datetime.date(2026, 9, 26)
        self.assertEqual(build.next_version(day, {"2026.09.26.01"}), "2026.09.26.02")
        self.assertEqual(build.next_version(day, {"", "2026.09.25.01"}), "2026.09.26")
        with self.assertRaises(build.BuildError):
            build.next_version(day, {"2026.09.27"})

    def test_the_index_keeps_the_newest_first_and_only_ten(self):
        index: dict = {}
        game = {"gameId": "genshin", "displayName": "Genshin Impact"}
        for day in range(1, 13):
            index = release.update_index(index, game, {"packVersion": f"2026.09.{day:02d}", "url": "u"})
        versions = [v["packVersion"] for v in index["packs"][0]["versions"]]
        self.assertEqual(len(versions), release.KEEP_VERSIONS)
        self.assertEqual(versions[0], "2026.09.12")
        self.assertEqual(index["updatedAt"], "2026-09-12T00:00:00Z")
        self.assertIn("2026.09.12", release.published_versions(index, "genshin"))

    def test_a_zip_is_the_same_bytes_every_time(self):
        with helpers.tempfile.TemporaryDirectory() as scratch:
            pack = helpers.pathlib.Path(scratch)
            (pack / "manifest.json").write_text('{"packVersion": "2026.09.25"}')
            (pack / "images").mkdir()
            (pack / "images" / "A.webp").write_bytes(b"x")
            first = release.zip_bytes(pack)
            (pack / "images" / "A.webp").touch()
            self.assertEqual(first, release.zip_bytes(pack))


class PictureTest(unittest.TestCase):
    def test_pictures_become_small_webp(self):
        for crop in ("none", "top-square"):
            data = images.normalise(helpers.png(size=(900, 1600)), crop)
            with images.Image.open(images.io.BytesIO(data)) as picture:
                self.assertEqual(picture.format, "WEBP")
                self.assertLessEqual(max(picture.size), 512)
            self.assertLessEqual(len(data), images.MAX_BYTES)

    def test_an_outfit_s_own_crop_is_used_and_a_change_of_crop_cuts_it_again(self):
        import tempfile

        tall = helpers.png(size=(700, 964))
        asked = []

        class Fake:
            def get(inner, url, *, fresh=True):
                asked.append(url)
                return tall

        outfit = assemble.Variant(name="CarlottaSplashingSummer", display="Splashing Summer", image_url="https://x/card.webp", image_crop="top-square")
        with tempfile.TemporaryDirectory() as folder:
            first = images.build([outfit], pathlib.Path(folder), {}, "none", Fake())
            with images.Image.open(pathlib.Path(folder) / "CarlottaSplashingSummer.webp") as picture:
                self.assertEqual(picture.width, picture.height, "the card's top square, not the whole card")
            images.build([outfit], pathlib.Path(folder), first.sources, "none", Fake())
            outfit.image_crop = None
            images.build([outfit], pathlib.Path(folder), first.sources, "none", Fake())
        self.assertEqual(first.sources["CarlottaSplashingSummer"]["crop"], "top-square")
        self.assertEqual(len(asked), 2, "read again only when the crop changed")

    def test_something_that_is_not_a_picture_is_refused(self):
        with self.assertRaises(Exception):
            images.normalise(b"not a picture", "none")


if __name__ == "__main__":
    unittest.main()


class HostilePictureTest(unittest.TestCase):
    """A picture from a source is data: only picture formats, and no bomb (audit P4)."""

    def test_a_format_that_is_not_a_picture_format_is_refused(self):
        eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
        with self.assertRaises((OSError, ValueError)):
            images.game_icon(eps)

    def test_a_picture_claiming_to_be_enormous_is_refused_as_unreadable(self):
        png = bytearray(helpers.png())
        png[16:24] = (60000).to_bytes(4, "big") + (60000).to_bytes(4, "big")
        png[29:33] = zlib.crc32(bytes(png[12:29])).to_bytes(4, "big")  # the header's own checksum
        with self.assertRaises(ValueError):
            images.game_icon(bytes(png))


class PublicZipTest(unittest.TestCase):
    """What goes into a public zip (audit P9)."""

    def test_a_link_in_a_pack_is_not_zipped(self):
        import tempfile, pathlib, zipfile, io, os
        with tempfile.TemporaryDirectory() as temp:
            pack = pathlib.Path(temp) / "pack"
            (pack / "images").mkdir(parents=True)
            (pack / "manifest.json").write_text('{"packVersion": "2026.09.25"}')
            secret = pathlib.Path(temp) / "secret.txt"
            secret.write_text("private")
            os.symlink(secret, pack / "images" / "x.webp")
            os.symlink(pathlib.Path(temp), pack / "linked")
            names = zipfile.ZipFile(io.BytesIO(release.zip_bytes(pack))).namelist()
        self.assertEqual(names, ["manifest.json"])

    def test_a_name_with_a_trailing_newline_is_not_an_id(self):
        self.assertFalse(names.is_valid_id("Foo\n"))
        self.assertTrue(names.is_valid_id("Foo"))

"""Formát v2 na čistě vymyšlených datech bez sítě."""

import contextlib
import copy
from datetime import date
import gzip
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import build_web
from state import State
from tests.helpers import TemporaryDirectory
from uloziste import Storage


CORE_COLS = ["id", "title", "title_english", "type", "episodes", "delka_min", "status",
             "aired_od", "aired_do", "season", "year", "source", "rating", "score", "scored_by",
             "members", "favorites", "rank", "popularity", "genres", "explicit_genres", "themes",
             "demographics", "studios", "producers", "img"]
AL_COLS = ["id", "al", "format", "zeme", "adult", "prumer", "popularita", "oblibeni", "tagy"]
AL_REMOVED = {"id", "tags", "recommendations", "staff", "characters", "idMal", "format",
              "countryOfOrigin", "isAdult", "averageScore", "popularity", "favourites"}


class BuildWebV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.day = date(2026, 11, 12)

    def build(self, mode="cele"):
        return build_web.build(self.root, self.day, mode, log=lambda line: None)

    def read(self, name):
        return json.loads((self.root / "web" / name).read_text(encoding="utf-8"))

    def snapshot(self):
        return {path.relative_to(self.root / "web").as_posix(): path.read_bytes()
                for path in (self.root / "web").rglob("*.json")}

    def populate(self):
        storage = Storage(self.root)
        first = {"title": "Papírové hodiny", "title_english": "Paper Clocks",
                 "title_japanese": "架空の紙時計", "title_synonyms": ["PAPÍROVÉ HODINY", "paper clocks",
                     "架空の紙時計", "Cesta tužky", "CESTA TUŽKY", "Straße", "STRASSE", "", "   "],
                 "type": "TV", "episodes": 8, "duration": "1 hr 2 min 30 sec",
                 "status": "Finished Airing", "aired": {"from": "2025-02-03T12:00:00Z", "to": "2025-03-04"},
                 "season": "winter", "year": 2025, "source": "Original", "rating": "PG",
                 "score": 6.25, "scored_by": 17, "members": 40, "favorites": 2, "rank": 12,
                 "popularity": 13, "synopsis": "   ", "img": "/images/anime/test/clock.jpg"}
        for index, key in enumerate(("genres", "explicit_genres", "themes", "demographics", "studios", "producers")):
            first[key] = [{"mal_id": 8701 + index, "name": "Vymyšlený slovník " + key}]
        second = {"title": "Město ořezávátek", "title_synonyms": ["Ořezávátkov"],
                  "status": "Finished Airing", "synopsis": None}
        # Zápis opačným pořadím ověřuje číselné řazení řádků při exportu.
        storage.put("tenrai", "list", 1000, second)
        storage.put("tenrai", "list", 999, first)
        person = lambda ident: {"mal_id": ident, "name": f"Vymyšlený člověk {ident}", "img": "/test.jpg"}
        storage.put("tenrai", "staff", 999, [
            {"person": person(6801), "positions": ["Key Animation (ep 1)", "Director (ep 2)",
                                                   "Key Animation (ep 4)", "Director (ep 3) (ep 4)"]},
            {"person": person(6802), "positions": []},
            {"person": person(6803), "positions": ["(ep 5)"]},
            {"person": person(6801), "positions": ["Director", "Key Animation (ep 9)"]}])
        actors = [{"person": person(pid), "language": language} for pid, language in
                  ((6804, "Japanese"), (6805, "English"), (6806, "Czech"), (6804, "Japanese"),
                   (6807, "Japanese"), (6808, "English"), (6804, "English"))]
        self.characters = [
            {"character": {"mal_id": 7801, "name": "Vymyšlená Tužka", "img": "/test.jpg"},
             "role": "Main", "favorites": 9, "voice_actors": actors},
            {"character": {"mal_id": 7802, "name": "Vymyšlená Guma"}, "role": "Supporting", "voice_actors": []}]
        storage.put("tenrai", "chars", 999, self.characters)
        storage.put("tenrai", "chars", 1000, [self.characters[1]])
        storage.put("tenrai", "recs", 1000, [[999, 2], [99999, 1]])
        storage.put("tenrai", "recs", 999, [[1000, 8], [99999, 5]])
        storage.put("tenrai", "stats", 999, {"watching": 2, "completed": 5, "on_hold": 1,
                    "dropped": 3, "plan_to_watch": 7, "total": 18, "scores": [{"score": 2, "votes": 11}]})
        storage.put("tenrai", "full", 999, {"relations": [
            {"relation": "Sequel", "entry": [{"mal_id": 1000, "type": "anime"},
                {"mal_id": 99999, "type": "anime"}, {"mal_id": 88888, "type": "manga"}]}]})
        self.al_first = {"id": 49002, "idMal": 999, "format": "TV_SHORT", "countryOfOrigin": "ZZ",
            "isAdult": True, "averageScore": 63, "popularity": 51, "favourites": 4,
            "title": {"romaji": "cesta tužky", "english": "Clock Parade", "native": "架空の紙時計"},
            "synonyms": ["CLOCK PARADE", "Jiná cesta", "PAPER CLOCKS", "strasse", ""],
            "description": " <b>Vymyšlený &amp; příběh</b><BR>Hodiny<br />&lt;tik&gt;<br/> konec. ",
            "tags": [[9703, 40, 0], [9702, 90, 1], [9701, 90, 0]],
            "recommendations": [[49003, 1000, 7], [49003, 1000, 7], [49999, 99999, 22]],
            "staff": [[6809, "Vymyšlená role", "Vymyšlený autor", "架空作者"]],
            "characters": [[7810, "MAIN", "Vymyšlený ciferník", None, [], None, None, 5,
                            [[6810, "Vymyšlený hlas", None, "JAPANESE"]]]],
            "studios": [[8710, "Vymyšlené studio AL", True, True]],
            "externalLinks": [{"site": "Vymyšlený web", "url": "https://priklad.invalid/hodiny"}],
            "relations": [["SEQUEL", 49003, 1000, "ANIME", "MOVIE"]],
            "status": "FINISHED", "episodes": 8, "genres": ["Vymyšlený žánr AL"]}
        self.al_second = {"id": 49003, "idMal": 1000, "title": {"romaji": "Město AL", "english": None},
            "synonyms": ["Město z pastelek"], "description": "<i>Záloha pro město.</i>",
            "tags": [], "recommendations": [[49002, 999, 3]], "staff": [], "characters": []}
        storage.put("anilist", "media", 49003, self.al_second)
        storage.put("anilist", "media", 49002, self.al_first)
        old = {**self.al_first, "id": 49001, "title": {"romaji": "Starý alias"}, "synonyms": [],
               "description": "Starý popis", "tags": [[9799, 99, 0]],
               "recommendations": [[49003, 1000, 99]], "staff": [], "characters": []}
        storage.put("anilist", "media", 49001, old)
        storage.put("anilist", "media", 49004, {"id": 49004, "idMal": 99999, "tags": [[9799, 99, 0]]})
        storage.put("anilist", "media", 49005, {"id": 49005, "idMal": None})
        storage.write_json("anilist", "tagy", [{"id": 9701, "name": "Vymyšlené tikání", "category": "Test",
                                             "isAdult": True, "description": "Vymyšlený popis tagu."}])
        storage.flush()
        state = State(self.root)
        state.data["list_pages"] = 4
        for page in (1, 2, 3):
            state.success("list", f"asc:{page}", self.day, pages=True)
        state.record("list", "asc:4", pages=True)[1] = 1
        state.success("list", "asc:9", self.day, pages=True)
        state.success("list", "desc:4", self.day, pages=True)
        state.save()
        al_state = State(self.root, "anilist")
        al_state.data["anilist"] = {"mode": "cele", "neighborhood": [999], "snapshot": {
            "done": True, "completed": "2026-11-04", "count": 5, "total": 6, "difference": 1,
            "incomplete": [[False, 1, 1]]}}
        al_state.save()
        return storage, state, al_state

    def test_core_a_slovniky_vsechny_sloupce(self):
        self.populate()
        self.build()
        core = self.read("core.json")
        self.assertEqual(set(core), {"pocet", "data"})
        self.assertEqual(core["pocet"], 2)
        self.assertEqual(set(core["data"]), set(CORE_COLS))
        expected = [999, "Papírové hodiny", "Paper Clocks", "TV", 8, 62.5, "Finished Airing",
                    "2025-02-03", "2025-03-04", "winter", 2025, "Original", "PG", 6.25, 17, 40,
                    2, 12, 13, [8701], [8702], [8703], [8704], [8705], [8706], "/images/anime/test/clock.jpg"]
        self.assertEqual([core["data"][key][0] for key in CORE_COLS], expected)
        self.assertEqual(core["data"]["id"], [999, 1000])
        self.assertTrue(all(len(values) == 2 for values in core["data"].values()))
        self.assertIsNone(core["data"]["type"][1])
        dictionaries = self.read("dict.json")
        self.assertEqual(set(dictionaries), {"genres", "explicit_genres", "themes", "demographics",
                                            "studios", "producers", "tags", "role", "jazyky"})
        for index, key in enumerate(("genres", "explicit_genres", "themes", "demographics", "studios", "producers")):
            self.assertEqual(dictionaries[key], {str(8701 + index): "Vymyšlený slovník " + key})
        self.assertEqual(dictionaries["tags"], {"9701": ["Vymyšlené tikání", "Test", True, "Vymyšlený popis tagu."]})

    def test_al_core_a_jednotny_vitez_duplicit(self):
        self.populate()
        self.build()
        core = self.read("al_core.json")
        self.assertEqual(set(core), {"pocet", "data"})
        self.assertEqual(core["pocet"], 2)
        self.assertEqual(set(core["data"]), set(AL_COLS))
        self.assertEqual([core["data"][key][0] for key in AL_COLS],
                         [999, 49002, "TV_SHORT", "ZZ", 1, 63, 51, 4, [9701, 90, 0, 9702, 90, 1, 9703, 40, 0]])
        self.assertEqual(core["data"]["id"], [999, 1000])
        self.assertEqual(core["data"]["adult"], [1, 0])
        self.assertTrue(all(len(values) == 2 for values in core["data"].values()))
        self.assertEqual(self.read("al_detail/0009.json")["999"]["al"], 49002)
        self.assertEqual(self.read("al_lide/0009.json")["999"]["staff"], self.al_first["staff"])

    def test_aliasy_a_zaloha_synopse_bez_html(self):
        self.populate()
        self.build()
        self.assertEqual(self.read("titles.json"), {
            "999": ["架空の紙時計", ["Cesta tužky", "Straße", "Clock Parade", "Jiná cesta"]],
            "1000": [None, ["Ořezávátkov", "Město AL", "Město z pastelek"]]})
        self.assertEqual(self.read("syn/0001.json"), {"999": "Vymyšlený & příběh\nHodiny\n<tik>\n konec."})
        self.assertEqual(self.read("syn/0002.json"), {"1000": "Záloha pro město."})

    def test_mal_synopse_ma_prednost_a_prazdne_zaznamy_chybi(self):
        storage, _, _ = self.populate()
        row = copy.deepcopy(storage.get("tenrai", "list", 999))
        row["synopsis"] = " Synopse MAL má přednost. "
        storage.put("tenrai", "list", 999, row)
        storage.put("tenrai", "list", 1000, {"title": "Samotný vymyšlený titul"})
        storage.put("anilist", "media", 49003, {"id": 49003, "idMal": 1000, "description": " <b> </b> "})
        storage.flush()
        self.build()
        self.assertEqual(self.read("syn/0001.json"), {"999": " Synopse MAL má přednost. "})
        self.assertFalse((self.root / "web/syn/0002.json").exists())
        self.assertNotIn("1000", self.read("titles.json"))

    def test_staff_zakladni_role_indexy_a_lide_bez_obrazku(self):
        self.populate()
        self.build()
        self.assertEqual(self.read("dict.json")["role"], ["(ep 5)", "Director", "Key Animation"])
        self.assertEqual(self.read("staff.json"), {"lide": {
            "6801": "Vymyšlený člověk 6801", "6802": "Vymyšlený člověk 6802", "6803": "Vymyšlený člověk 6803"},
            "anime": {"999": [[6801, 2, 1], [6802], [6803, 0]]}})

    def test_cast_a_chars_vsechny_jazyky_a_ploche_indexy(self):
        self.populate()
        self.build()
        self.assertEqual(self.read("dict.json")["jazyky"], ["Czech", "English", "Japanese"])
        chars = self.read("chars/0000.json")
        self.assertEqual(chars, {"postavy": {"7801": "Vymyšlená Tužka", "7802": "Vymyšlená Guma"},
            "lide": {str(pid): f"Vymyšlený člověk {pid}" for pid in range(6804, 6809)},
            "anime": {"999": [[7801, 1, 9, 6804, 2, 6805, 1, 6806, 0, 6804, 2, 6807, 2, 6808, 1, 6804, 1],
                              [7802, 0, 0]]}})
        cast = self.read("cast.json")
        self.assertEqual(cast, {"postavy": chars["postavy"],
            "lide": {str(pid): f"Vymyšlený člověk {pid}" for pid in (6804, 6805, 6807, 6808)},
            "anime": {"999": [[7801, 1, [6804, 6807], [6805, 6808, 6804]], [7802, 0, [], []]],
                      "1000": [[7802, 0, [], []]]}})
        self.assertEqual(self.read("chars/0001.json"), {"postavy": {"7802": "Vymyšlená Guma"}, "lide": {},
                                                       "anime": {"1000": [[7802, 0, 0]]}})

    def test_dabing_bez_jazyka_nezhodi_stavbu(self):
        self.populate()
        storage = Storage(self.root)
        storage.put("tenrai", "chars", 1000, [{"character": {"mal_id": 7803, "name": "Vymyšlená Tabule"},
            "voice_actors": [{"language": None, "person": {"mal_id": 6809, "name": "Vymyšlený člověk 6809"}}]}])
        storage.flush()
        self.build()
        languages = self.read("dict.json")["jazyky"]
        self.assertEqual(languages, ["Czech", "English", "Japanese", "Unknown"])
        self.assertEqual(self.read("chars/0001.json")["anime"]["1000"][-1][3:], [6809, 3])

    def test_recs_stats_rel_a_al_detail_lide(self):
        self.populate()
        self.build()
        self.assertEqual(self.read("recs.json"), {"mal": [999, 1000, 8, 1000, 999, 2],
                                                 "al": [999, 1000, 7, 1000, 999, 3]})
        self.assertEqual(self.read("stats.json"), {"999": [2, 5, 1, 3, 7, 18, [0, 11, 0, 0, 0, 0, 0, 0, 0, 0]]})
        self.assertEqual(self.read("rel.json"), {"999": [["Sequel", [1000]]]})
        for ident, row, block in ((999, self.al_first, "0009"), (1000, self.al_second, "0010")):
            detail = self.read(f"al_detail/{block}.json")[str(ident)]
            self.assertEqual(detail, {**{key: value for key, value in row.items() if key not in AL_REMOVED}, "al": row["id"]})
            self.assertTrue(AL_REMOVED.isdisjoint(detail))
            self.assertEqual(self.read(f"al_lide/{block}.json"),
                             {str(ident): {key: row[key] for key in ("staff", "characters")}})

    def test_meta_soubory_bloky_velikosti_a_log_pouze_pocty(self):
        self.populate()
        lines = []
        meta = build_web.build(self.root, self.day, log=lines.append)
        self.assertEqual(meta, self.read("meta.json"))
        self.assertEqual(set(meta), {"verze", "datum", "pocet", "bloky", "uplnost", "chyb", "preskoceno",
                                     "pryc", "al_tagy", "anilist", "velikosti"})
        self.assertEqual((meta["verze"], meta["pocet"], meta["datum"], meta["al_tagy"]), (2, 2, "2026-11-12", "cele"))
        self.assertEqual(meta["bloky"], {"syn": 500, "chars": 1000, "al_detail": 100, "al_lide": 100})
        self.assertEqual(meta["uplnost"], {"list": 0.75, "recs": 1.0, "full": 0.5, "staff": 0.5,
                                          "chars": 1.0, "stats": 0.5, "anilist": 1.0})
        self.assertEqual(meta["anilist"], {"rezim": "cele", "datum": "2026-11-04", "pocet": 5,
                                          "celkem": 6, "rozdil": 1, "hotovo": True, "neuplne_rezy": 1})
        self.assertEqual([meta[key] for key in ("chyb", "preskoceno", "pryc")], [0, 0, 0])
        files = self.snapshot()
        self.assertEqual(set(files), {"core.json", "al_core.json", "dict.json", "titles.json", "recs.json",
            "staff.json", "cast.json", "stats.json", "rel.json", "meta.json", "syn/0001.json", "syn/0002.json",
            "chars/0000.json", "chars/0001.json", "al_detail/0009.json", "al_detail/0010.json",
            "al_lide/0009.json", "al_lide/0010.json"})
        self.assertEqual(set(meta["velikosti"]), set(files) - {"meta.json"})
        for name, sizes in meta["velikosti"].items():
            self.assertEqual(sizes, [len(files[name]), len(gzip.compress(files[name], compresslevel=9, mtime=0))], name)
        self.assertEqual(lines, [f"stav: staženo 2, pryč 0, velikost {sum(map(len, files.values()))} bajtů"])

    def test_list_pages_fallback_a_nepocita_jine_stranky(self):
        _, state, _ = self.populate()
        self.assertEqual(self.build()["uplnost"]["list"], 0.75)
        for pages in (0, None):
            if pages is None:
                state.data.pop("list_pages")
            else:
                state.data["list_pages"] = pages
            state.save()
            self.assertEqual(self.build()["uplnost"]["list"], 1.0)
        state.data["list_pages"] = 2
        state.save()
        self.assertEqual(self.build()["uplnost"]["list"], 1.0)

    def test_okoli_omezuje_vsechny_casti_anilistu(self):
        self.populate()
        meta = self.build("okoli")
        self.assertEqual(meta["al_tagy"], "okoli")
        self.assertEqual(meta["uplnost"]["anilist"], 1.0)
        self.assertEqual(self.read("al_core.json")["data"]["id"], [999])
        self.assertEqual(self.read("recs.json")["al"], [999, 1000, 7])
        self.assertEqual(self.read("titles.json")["1000"], [None, ["Ořezávátkov"]])
        self.assertFalse((self.root / "web/syn/0002.json").exists())
        for name in ("al_detail", "al_lide"):
            self.assertEqual(set(self.read(f"{name}/0009.json")), {"999"})
            self.assertFalse((self.root / "web" / name / "0010.json").exists())
        self.assertEqual(self.read("core.json")["pocet"], 2)
        self.assertEqual(self.read("syn/0001.json")["999"], "Vymyšlený & příběh\nHodiny\n<tik>\n konec.")
        self.build()
        self.assertEqual(self.read("al_core.json")["data"]["id"], [999, 1000])
        self.assertEqual(self.read("syn/0002.json"), {"1000": "Záloha pro město."})

    def test_okoli_je_sjednoceni_stavu_a_aktualnich_anime(self):
        storage, _, _ = self.populate()
        row = copy.deepcopy(storage.get("tenrai", "list", 1000))
        row["status"] = "Currently Airing"
        storage.put("tenrai", "list", 1000, row)
        storage.flush()
        self.build("okoli")
        self.assertEqual(self.read("al_core.json")["data"]["id"], [999, 1000])

    def test_cli_okoli_vychozi_cele_a_auto_se_odmita(self):
        self.populate()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build_web.main(["--data", str(self.root), "--al-tagy", "okoli"]), 0)
        self.assertEqual(self.read("al_core.json")["data"]["id"], [999])
        for day in ("2026-11-03", "2026-11-11", "2026-11-12", "2030-01-01"):
            with self.subTest(day=day), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build_web.main(["--data", str(self.root), "--dnes", day]), 0)
                self.assertEqual(self.read("meta.json")["al_tagy"], "cele")
                self.assertEqual(self.read("al_core.json")["data"]["id"], [999, 1000])
                meta = build_web.build(self.root, date.fromisoformat(day), log=lambda line: None)
                self.assertEqual(meta["al_tagy"], "cele")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            build_web.main(["--data", str(self.root), "--al-tagy", "auto"])
        self.assertEqual(error.exception.code, 2)
        with self.assertRaises(ValueError):
            self.build("auto")

    def test_determinismus_a_vymena_odstrani_stare_soubory(self):
        self.populate()
        self.build()
        before = self.snapshot()
        for name in ("stare.json", "al/0001.json", "al_tags.json", "syn/9999.json"):
            path = self.root / "web" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        self.build()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.root.glob(".web-*")), [])
        self.assertFalse((self.root / "web/al").exists())

    def test_pryc_a_nestazene_nejsou_nikde_a_stavy_se_scitaji(self):
        storage, state, al_state = self.populate()
        state.failure("stats", 1000, self.day, 404)
        state.failure("full", 1000, self.day, 404)
        state.record("staff", 999)[1:3] = [5, self.day.toordinal() + 1]
        state.record("list", 88888)[1:3] = [5, self.day.toordinal() + 1]
        state.save()
        al_state.record("al_staff", 49002)[1:3] = [5, self.day.toordinal() + 1]
        al_state.save()
        for endpoint, row in (("recs", [[999, 80]]), ("staff", []), ("chars", []), ("stats", {}), ("full", {})):
            storage.put("tenrai", endpoint, 88888, row)
        storage.flush()
        meta = self.build()
        self.assertEqual((meta["pocet"], meta["pryc"], meta["chyb"], meta["preskoceno"]), (1, 1, 2, 2))
        self.assertEqual(self.read("core.json")["data"]["id"], [999])
        self.assertEqual(self.read("al_core.json")["data"]["id"], [999])
        self.assertEqual(self.read("recs.json"), {"mal": [], "al": []})
        self.assertEqual(self.read("rel.json"), {"999": []})
        self.assertEqual(set(self.read("cast.json")["anime"]), {"999"})
        for name in ("staff.json", "stats.json", "titles.json"):
            value = self.read(name)
            self.assertEqual(set(value.get("anime", value)), {"999"})
        for name in ("syn/0002.json", "chars/0001.json", "al_detail/0010.json", "al_lide/0010.json"):
            self.assertFalse((self.root / "web" / name).exists())

    def test_prazdny_export_a_nulova_uplnost(self):
        meta = self.build()
        self.assertEqual(meta["pocet"], 0)
        self.assertTrue(all(value == 0.0 for value in meta["uplnost"].values()))
        self.assertEqual(self.read("core.json"), {"pocet": 0, "data": {key: [] for key in CORE_COLS}})
        self.assertEqual(self.read("al_core.json"), {"pocet": 0, "data": {key: [] for key in AL_COLS}})
        for name in ("syn", "chars", "al_detail", "al_lide"):
            self.assertFalse((self.root / "web" / name).exists())
        self.assertEqual(self.read("titles.json"), {})
        self.assertEqual(self.read("recs.json"), {"mal": [], "al": []})
        self.assertEqual(self.read("staff.json"), {"lide": {}, "anime": {}})
        self.assertEqual(self.read("cast.json"), {"postavy": {}, "lide": {}, "anime": {}})
        self.assertEqual(self.read("dict.json")["role"], [])
        self.assertEqual(self.read("dict.json")["jazyky"], [])

    def test_limit_na_soubor_zachova_stary_web_a_uklidi_staging(self):
        storage, _, _ = self.populate()
        self.build()
        before = self.snapshot()
        with patch("build_web.MAX_BYTES", 1), self.assertRaises(ValueError):
            self.build()
        self.assertEqual(self.snapshot(), before)
        # Velká synopse ověřuje limit i u bloků za již zapsaným jádrem.
        row = copy.deepcopy(storage.get("tenrai", "list", 999))
        row["synopsis"] = "x" * 10000
        storage.put("tenrai", "list", 999, row)
        storage.flush()
        with patch("build_web.MAX_BYTES", 5000), self.assertRaises(ValueError):
            self.build()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.root.glob(".web-*")), [])

    def test_limit_plati_i_pro_meta(self):
        self.build()
        before = self.snapshot()
        limit = len(before["meta.json"]) - 1
        self.assertGreater(limit, max(len(value) for key, value in before.items() if key != "meta.json"))
        with patch("build_web.MAX_BYTES", limit), self.assertRaises(ValueError):
            self.build()
        self.assertEqual(self.snapshot(), before)

    def test_chyba_vymeny_obnovi_web_a_main_nevypise_obsah(self):
        self.populate()
        self.build()
        before = self.snapshot()
        original = build_web.replace

        def fail(source, target):
            if Path(source).name.startswith(".web-novy-"):
                raise OSError("TAJNY_POPIS")
            original(source, target)

        output = io.StringIO()
        with patch("build_web.replace", side_effect=fail), contextlib.redirect_stdout(output):
            self.assertEqual(build_web.main(["--data", str(self.root)]), 1)
        self.assertEqual(output.getvalue(), "stav: chyba sestavení webových dat\n")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.root.glob(".web-*")), [])

    def test_safe_remove_a_web_symlink(self):
        outside = self.root.parent / "cizi-adresar"
        with self.assertRaises(ValueError):
            build_web.safe_remove(outside, self.root)
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ValueError):
                build_web.safe_remove(self.root / "web", self.root)
            with self.assertRaises(ValueError):
                self.build()
        self.assertFalse((self.root / "web").exists())


if __name__ == "__main__":
    unittest.main()

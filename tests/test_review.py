"""Regrese odolnosti, souběžných zdrojů a úplných dat; výhradně vymyšlené odpovědi."""

import copy
from datetime import date, timedelta
import http.client
import io
import json
from pathlib import Path
import re
import socket
import ssl
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import parse_qs, urlsplit

from anilist import AniList, CONNECTIONS, FIELDS, compact, new_slice
import build_web
from fronta import Tenrai
import osobni
from state import State
import sync
from tempo import Client, Limiter, Response, StopNight, transport
import tenrai
from tests.helpers import Clock, Server, TemporaryDirectory, fixture, media
from uloziste import Storage


def character(ident):
    return {"role": "MAIN", "node": {"id": ident,
            "name": {"full": f"Vymyšlená postava {ident}", "native": "Znak", "alternative": ["Přezdívka"]},
            "gender": "Female", "age": "20", "favourites": 7},
            "voiceActors": [{"id": 9001, "name": {"full": "Vymyšlený herec", "native": "Jméno"},
                             "languageV2": "JAPANESE"}]}


def staff(ident):
    return {"role": "Director", "node": {"id": ident, "name": {
        "full": f"Vymyšlená osoba {ident}", "native": "Jméno"}}}


def full_media(ident=1, mal=1101):
    row = media(ident)
    row.update(idMal=mal, type="ANIME", source="ORIGINAL", countryOfOrigin="JP", isLicensed=True,
               title={"romaji": "Vymyšlené anime", "english": "Imaginary anime", "native": "Znak"},
               synonyms=["Vymyšlené synonymum"], description="Vymyšlený popis.",
               startDate={"year": 2026, "month": 1, "day": 2},
               endDate={"year": 2026, "month": 3, "day": 4}, season="WINTER", seasonYear=2026,
               episodes=12, duration=24, genres=["Fantasy"], averageScore=70, meanScore=71,
               favourites=8, trending=3, hashtag="#vymyslene", coverImage={"extraLarge":
               "https://priklad.invalid/obal.jpg", "color": "#123456"},
               bannerImage="https://priklad.invalid/pruh.jpg", trailer={"id": "vymysleny", "site": "youtube"},
               nextAiringEpisode={"airingAt": 123456, "episode": 3},
               rankings=[{"rank": 10, "type": "RATED", "format": "TV", "year": 2026,
                          "season": "WINTER", "allTime": False, "context": "Vymyšlený kontext"}],
               stats={"scoreDistribution": [{"score": 70, "amount": 5}],
                      "statusDistribution": [{"status": "CURRENT", "amount": 3}]},
               studios={"edges": [{"isMain": True, "node": {"id": 8001,
                        "name": "Vymyšlené studio", "isAnimationStudio": True}}]},
               externalLinks=[{"site": "Vymyšlený web", "url": "https://priklad.invalid/web",
                               "type": "INFO", "language": "Japanese"}],
               streamingEpisodes=[{"title": "Vymyšlená epizoda", "thumbnail": "https://priklad.invalid/nahled",
                                   "url": "https://priklad.invalid/epizoda", "site": "Vymyšlená služba"}],
               tags=[{"id": 8002, "rank": 50, "isMediaSpoiler": True}],
               characters={"pageInfo": {"hasNextPage": False}, "edges": [character(1)]},
               staff={"pageInfo": {"hasNextPage": False}, "edges": [staff(1)]})
    return row


class ConnectionsServer(Server):
    def __init__(self, rows=None):
        super().__init__(rows)
        self.pages = {}
        self.batch_status = None
        self.null_ids = set()
        self.alias_calls = []
        self.interrupt = None

    def __call__(self, method, url, headers, payload, timeout):
        if payload and "a0: Media" in payload["query"]:
            aliases = re.findall(r"(a\d+): Media\(id:(\d+)\) \{ (\w+)\(page:(\d+),perPage:25", payload["query"])
            assert aliases
            self.calls.append((url, copy.deepcopy(payload)))
            self.alias_calls.append(aliases)
            if self.interrupt and self.interrupt(aliases):
                raise StopNight()
            if self.batch_status and len(aliases) > 1:
                return Response(self.batch_status)
            data = {}
            for alias, ident, name, page in aliases:
                key = (int(ident), name, int(page))
                connection = self.pages.get(key, {"pageInfo": {"hasNextPage": False},
                                                  "nodes" if name == "recommendations" else "edges": []})
                data[alias] = None if int(ident) in self.null_ids else {name: copy.deepcopy(connection)}
            return Response(200, {"data": data})
        return super().__call__(method, url, headers, payload, timeout)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.today = date(2026, 11, 3)
        self.clock = Clock()

    def client(self, source, send):
        return Client(source, Limiter(20, self.clock.now, self.clock.sleep), send, log=lambda line: None)

    def runner(self, server, state=None, storage=None):
        return AniList(self.client("anilist", server), state or State(self.root, "anilist"),
                       storage or Storage(self.root, "anilist"), self.today, float("inf"), log=lambda line: None)

    def run_sync(self, server, *args):
        options = sync.parser().parse_args(["--data", str(self.root), "--dnes", "2026-11-03", *args])
        return sync.run(options, server, self.clock.now, self.clock.sleep, self.clock.utcnow, log=lambda line: None)

    def test_vsechny_opakovatelne_vyjimky_transportu_i_klienta(self):
        errors = [OSError("TAJNY_TITUL"), ssl.SSLError("TAJNY_TITUL"),
                  urllib.error.URLError("TAJNY_TITUL"), ConnectionError("TAJNY_TITUL"),
                  socket.timeout("TAJNY_TITUL"), http.client.HTTPException("TAJNY_TITUL"),
                  http.client.IncompleteRead(b"TAJNY_TITUL"), http.client.BadStatusLine("TAJNY_TITUL"),
                  json.JSONDecodeError("TAJNY_TITUL", "TAJNY_TITUL", 0),
                  UnicodeDecodeError("utf-8", b"\xff", 0, 1, "TAJNY_TITUL")]
        for error in errors:
            with self.subTest(typ=type(error).__name__):
                with patch("tempo.urllib.request.urlopen", side_effect=error):
                    self.assertEqual(transport("GET", "https://priklad.invalid", {}, None, 30).status, 599)
                client = self.client("tenrai", lambda *args: (_ for _ in ()).throw(error))
                self.assertEqual(client.request("full", "https://priklad.invalid").status, 599)
                self.assertEqual(client.counts["full"]["dotazu"], 3)

    def test_neplatne_json_a_unicode_v_200(self):
        for content in (b"<html>TAJNY_TITUL</html>", b"\xff"):
            stream = io.BytesIO(content)
            stream.status, stream.headers = 200, {}
            with patch("tempo.urllib.request.urlopen", return_value=stream):
                self.assertEqual(transport("GET", "https://priklad.invalid", {}, None, 30).status, 599)

    def test_neobjektove_telo_a_graphql_data_jsou_502(self):
        for source, bodies in (("tenrai", [None, [], "html", 42]),
                               ("anilist", [{}, {"data": None}, {"data": []}, {"data": "html"}])):
            for body in bodies:
                with self.subTest(zdroj=source, telo=type(body).__name__):
                    client = self.client(source, lambda *args: Response(200, body))
                    self.assertEqual(client.request("full", "https://priklad.invalid").status, 502)
                    self.assertEqual(client.counts["full"]["dotazu"], 3)

    def test_posun_hodin_ceka_nejvyse_interval(self):
        state = State(self.root)
        state.data["tenrai_last_start"] = self.clock.utcnow().timestamp() + 86400
        limiter = Limiter(108, self.clock.now, self.clock.sleep, state, self.clock.utcnow)
        limiter.start()
        self.assertEqual(self.clock.sleeps, [60 / 108])

    def test_tenrai_null_doporuceni_a_necekany_tvar(self):
        self.assertEqual(tenrai.reduce("recs", [{"entry": None}, {}, {"entry": {"mal_id": "3"}},
                         {"entry": {"mal_id": 3}, "votes": 4}]), [[3, 4]])
        state, storage = State(self.root), Storage(self.root, "tenrai")
        server = Server()

        def send(method, url, *args):
            return Response(200, {"data": {"entry": None}}) if "/1/recommendations" in url else server(method, url, *args)

        job = Tenrai(self.client("tenrai", send), state, storage, self.today, float("inf"))
        job.fetch_item("recs", 1)
        job.fetch_item("recs", 2)
        self.assertEqual(state.get("recs", 1)[1], 1)
        self.assertTrue(state.get("recs", 2)[0])
        with patch("tenrai.reduce", side_effect=KeyError("TAJNY_TITUL")):
            job.fetch_item("full", 1)
        self.assertEqual(state.get("full", 1)[1], 1)
        self.assertFalse(state.get("full", 1)[3])

    def test_trvale_vadna_prostredni_stranka_neblokuje_zbytek(self):
        pages = []

        def send(method, url, *args):
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            pages.append(page)
            if page == 2:
                return Response(500)
            return Response(200, {"data": [{"mal_id": page, "title": "Vymyšlené anime"}],
                                  "pagination": {"last_visible_page": 3, "has_next_page": page < 3}})

        state, storage = State(self.root), Storage(self.root)
        for night in range(5):
            day = self.today + timedelta(days=night)
            Tenrai(self.client("tenrai", send), state, storage, day, float("inf")).initial_list()
            self.assertEqual(state.data["list_complete"], night == 4)
        self.assertTrue(state.get("list", "asc:3", pages=True)[0])
        self.assertEqual(pages.count(1), 1)
        self.assertEqual(pages.count(3), 1)
        self.assertEqual(pages.count(2), 15)
        pages.clear()
        Tenrai(self.client("tenrai", send), state, storage, self.today + timedelta(days=6), float("inf")).initial_list()
        self.assertEqual(pages, [])
        self.assertTrue(state.data["list_complete"])

    def test_prvni_noc_kazda_stranka_prave_jednou(self):
        server, pages = Server(), []

        def send(method, url, *args):
            if urlsplit(url).path == "/v1/anime":
                params = parse_qs(urlsplit(url).query)
                self.assertEqual(params["sort"], ["asc"])
                self.assertNotIn("status", params)
                page = int(params["page"][0])
                pages.append(page)
                return Response(200, {"data": [{"mal_id": page, "members": 1}],
                                      "pagination": {"has_next_page": page < 3}})
            return server(method, url, *args)

        Tenrai(self.client("tenrai", send), State(self.root), Storage(self.root), self.today, float("inf")).run()
        self.assertEqual(pages, [1, 2, 3])

    def test_externi_a_streamovaci_url_zustavaji_ostatni_mizi(self):
        row = {"url": "smazat", "external": [{"name": "Web", "url": "https://priklad.invalid/web",
                                               "nested": {"url": "smazat"}}],
               "streaming": [{"name": "Služba", "url": "https://priklad.invalid/stream"}],
               "relations": [{"entry": [{"url": "smazat", "mal_id": 1}]}]}
        result = tenrai.reduce("full", row)
        self.assertEqual(result["external"][0]["url"], "https://priklad.invalid/web")
        self.assertEqual(result["streaming"][0]["url"], "https://priklad.invalid/stream")
        self.assertEqual(result["external"][0]["nested"], {})
        self.assertNotIn("url", result["relations"][0]["entry"][0])

    def test_aktivni_vysilane_den_ohlasene_tyden(self):
        state, storage, server = State(self.root), Storage(self.root), Server()
        state.data.update(list_complete=True, list_last=0)
        for ident, status in ((1, "Currently Airing"), (2, "Not yet aired")):
            storage.put("tenrai", "list", ident, {"status": status})
            for endpoint in tenrai.ENDPOINTS[1:]:
                state.success(endpoint, ident, self.today - timedelta(days=6))
        job = Tenrai(self.client("tenrai", server), state, storage, self.today, float("inf"))
        with patch.object(job, "scan"):
            job.run()
        urls = [url for url, _ in server.calls]
        self.assertTrue(any("/1/recommendations" in url for url in urls))
        self.assertTrue(any("/1/statistics" in url for url in urls))
        self.assertFalse(any("/2/" in url for url in urls))
        server.calls.clear()
        job.today += timedelta(days=1)
        job.queue.today = job.today
        with patch.object(job, "scan"):
            job.run()
        self.assertTrue(any("/2/recommendations" in url for url, _ in server.calls))
        self.assertTrue(any("/2/statistics" in url for url, _ in server.calls))

    def test_zustavaji_vsechna_pole_a_presny_tvar_zusteni(self):
        original = full_media()
        result = compact(original)
        for key in set(original) - {"tags", "recommendations", "relations", "characters", "staff", "studios"}:
            self.assertEqual(original[key], result[key], key)
            self.assertIn(key, FIELDS)
        self.assertEqual(result["characters"], [[1, "MAIN", "Vymyšlená postava 1", "Znak", ["Přezdívka"],
                         "Female", "20", 7, [[9001, "Vymyšlený herec", "Jméno", "JAPANESE"]]]])
        self.assertEqual(result["staff"], [[1, "Director", "Vymyšlená osoba 1", "Jméno"]])
        self.assertEqual(result["studios"], [[8001, "Vymyšlené studio", True, True]])
        self.assertEqual(result["tags"], [[8002, 50, 1]])
        for text in ("status(version: 2)", "source(version: 3)", "characters(perPage: 25", "staff(perPage: 25"):
            self.assertIn(text, FIELDS)

    def test_jedno_vadne_medium_nezastavi_snimek(self):
        bad, good = full_media(1), full_media(2)
        bad["characters"]["edges"] = [{"node": None}]
        runner = self.runner(Server([bad, good]))
        runner.run_snapshot()
        self.assertEqual(runner.state.get("anilist", 1)[1], 1)
        self.assertIsNotNone(runner.storage.get("anilist", "media", 2))
        self.assertTrue(runner.snapshot["done"])
        self.assertFalse(runner.state.get("anilist", 1)[3])

    def test_kazda_chyba_davky_rozpadne_vsechny_typy_spojeni(self):
        for status in (400, 403, 404, 500, 599):
            for name in CONNECTIONS:
                with self.subTest(stav=status, spojeni=name):
                    server = ConnectionsServer()
                    server.batch_status = status
                    runner = self.runner(server, State(self.root, "anilist"), Storage(self.root, "anilist"))
                    for ident in (1, 2):
                        row = full_media(ident, ident + 1000)
                        row[name]["pageInfo"]["hasNextPage"] = True
                        runner.save_media(row)
                    runner.finish_recommendations()
                    self.assertEqual(runner.al["pending"], {})
                    self.assertTrue(any(len(batch) == 2 for batch in server.alias_calls))
                    self.assertEqual(sum(len(batch) == 1 for batch in server.alias_calls), 2)

    def test_chybejici_alias_ma_failure_a_zmizi_z_cekajicich(self):
        for name, (endpoint, _, _, _) in CONNECTIONS.items():
            server = ConnectionsServer()
            server.null_ids = {1}
            runner = self.runner(server)
            row = full_media()
            row[name]["pageInfo"]["hasNextPage"] = True
            runner.save_media(row)
            runner.finish_recommendations()
            self.assertEqual(runner.al["pending"], {})
            self.assertTrue(runner.state.get(endpoint, 1)[1])
            count = len(server.calls)
            runner.finish_recommendations()
            self.assertEqual(len(server.calls), count)

    def test_postavy_tri_strany_stab_pet_obnova_po_padu(self):
        server = ConnectionsServer()
        row = full_media()
        for name, total, make in (("characters", 3, character), ("staff", 5, staff)):
            row[name] = {"pageInfo": {"hasNextPage": True}, "edges": [make(i) for i in range(1, 26)]}
            for page in range(2, total + 1):
                server.pages[(1, name, page)] = {"pageInfo": {"hasNextPage": page < total},
                                                "edges": [make(i) for i in range((page - 1) * 25 + 1, page * 25 + 1)]}
        runner = self.runner(server)
        runner.save_media(row)
        server.interrupt = lambda aliases: any(name == "characters" and page == "3" for _, _, name, page in aliases)
        with self.assertRaises(StopNight):
            runner.finish_recommendations()
        runner.storage.flush()
        runner.state.save()
        saved = State(self.root, "anilist")
        self.assertEqual(saved.data["anilist"]["pending"], {"1": {"characters": 3, "staff": 2}})
        server.interrupt = None
        server.alias_calls.clear()
        resumed = self.runner(server, saved, Storage(self.root, "anilist"))
        resumed.finish_recommendations()
        result = resumed.storage.get("anilist", "media", 1)
        self.assertEqual([entry[0] for entry in result["characters"]], list(range(1, 76)))
        self.assertEqual([entry[0] for entry in result["staff"]], list(range(1, 126)))
        self.assertEqual(resumed.al["pending"], {})
        self.assertEqual([(name, int(page)) for batch in server.alias_calls for _, _, name, page in batch],
                         [("characters", 3), ("staff", 2), ("staff", 3), ("staff", 4), ("staff", 5)])

    def test_duplicitni_okraj_stran_a_ruzna_role_stejne_osoby(self):
        server, runner = ConnectionsServer(), None
        runner = self.runner(server)
        row = full_media()
        row["staff"]["pageInfo"]["hasNextPage"] = True
        runner.save_media(row)
        second_role = staff(1)
        second_role["role"] = "Script"
        server.pages[(1, "staff", 2)] = {"pageInfo": {"hasNextPage": False},
                                        "edges": [staff(1), second_role, staff(2)]}
        runner.finish_recommendations()
        self.assertEqual([(item[0], item[1]) for item in runner.storage.get("anilist", "media", 1)["staff"]],
                         [(1, "Director"), (1, "Script"), (2, "Director")])

    def test_chyba_zpracovani_davky_rozpadne_i_ostatni_aliasy(self):
        server = ConnectionsServer()
        runner = self.runner(server)
        for ident in (1, 2):
            row = full_media(ident)
            row["staff"]["pageInfo"]["hasNextPage"] = True
            runner.save_media(row)
        server.pages[(2, "staff", 2)] = {"pageInfo": {"hasNextPage": False}, "edges": [{"node": None}]}
        runner.finish_recommendations()
        self.assertEqual([len(batch) for batch in server.alias_calls], [2, 1, 1])
        self.assertEqual(runner.al["pending"], {"2": {"staff": 2}})
        self.assertEqual(runner.state.get("al_staff", 2)[1], 1)
        self.assertFalse(runner.state.get("al_staff", 2)[3])
        self.assertTrue(runner.state.get("al_staff", 1)[0])
        runner.finish_recommendations()
        self.assertEqual(len(server.alias_calls), 3)

    def test_neplatne_medium_v_davce_neprebije_failure_uspechem(self):
        runner = self.runner(Server())
        with patch.object(runner, "query", return_value=Response(200, {"data": {"Page": {"media": [{}]}}})):
            runner.fetch_mal([1101])
        self.assertEqual(runner.state.get("al_mal", 1101)[1], 1)
        self.assertFalse(runner.state.get("al_mal", 1101)[0])

    def test_vadny_zaznam_zmen_neblokuje_ostatni(self):
        runner = self.runner(Server())
        changed, older = runner.track_changes([None, {"id": 1, "idMal": 1101, "updatedAt": []},
                                               {"id": 2, "idMal": 1102, "updatedAt": 20}], {1101, 1102}, 10)
        self.assertEqual(changed, {1102})
        self.assertFalse(older)
        self.assertEqual(runner.state.get("al_mal", 1101)[1], 1)

    def test_chyba_400_zname_prostredni_stranky_neukonci_pruchod(self):
        pages = []

        def send(method, url, *args):
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            pages.append(page)
            if page == 2:
                return Response(400)
            return Response(200, {"data": [{"mal_id": page}],
                                  "pagination": {"last_visible_page": 3, "has_next_page": page < 3}})

        state = State(self.root)
        Tenrai(self.client("tenrai", send), state, Storage(self.root), self.today, float("inf")).initial_list()
        self.assertEqual(pages, [1, 2, 3])
        self.assertFalse(state.data["list_complete"])
        self.assertTrue(state.get("list", "asc:3", pages=True)[0])

    def test_otevrena_horni_mez_se_nedeli_po_jedne(self):
        row = full_media()
        row["popularity"] = 100000
        server = Server([row])
        runner = self.runner(server)
        part = new_slice(False, 100000, None)
        part.update(page=100, reverse=True, observed_lo=100000, observed_hi=100000)
        runner.snapshot.update(todo=[part], incomplete=[], tags_done=True)
        count = 0

        def query(*args, **kwargs):
            nonlocal count
            count += 1
            if count > 1:
                raise StopNight()
            return Response(200, {"data": {"Page": {"media": [row], "pageInfo": {"hasNextPage": True}}}})

        with patch.object(runner, "query", query), self.assertRaises(StopNight):
            runner.run_snapshot()
        self.assertEqual([(part["lo"], part["hi"]) for part in runner.snapshot["todo"]],
                         [(100000, 200000), (200001, None)])

    def test_oddeleni_stavu_a_bloku_stridajici_checkpointy(self):
        states = {name: State(self.root, name) for name in ("tenrai", "anilist")}
        stores = {name: Storage(self.root, name) for name in states}
        for turn, name in enumerate(("tenrai", "anilist", "tenrai", "anilist")):
            states[name].success("list" if name == "tenrai" else "anilist", turn, self.today)
            stores[name].put(name, "list" if name == "tenrai" else "media", turn, {"id": turn})
            stores[name].flush()
            states[name].save()
        mal, al = State(self.root), State(self.root, "anilist")
        self.assertEqual(set(mal.data["items"]["list"]), {"0", "2"})
        self.assertEqual(set(al.data["items"]["anilist"]), {"1", "3"})
        self.assertNotIn("anilist", mal.data)
        self.assertNotIn("pages", al.data)
        self.assertNotIn("tenrai_days", al.data)
        self.assertEqual(set(dict(Storage(self.root).items("tenrai", "list"))), {0, 2})
        self.assertEqual(set(dict(Storage(self.root).items("anilist", "media"))), {1, 3})
        with self.assertRaises(ValueError):
            stores["anilist"].put("tenrai", "list", 4, {})
        with self.assertRaises(ValueError):
            stores["tenrai"].write_json("anilist", "meta", {})

    def test_samostatne_behy_se_vnorenym_checkpointem(self):
        original = State.save
        interleaved = False
        al_files = {}

        def save(state):
            nonlocal interleaved, al_files
            original(state)
            if state.source == "tenrai" and not interleaved:
                interleaved = True
                self.assertEqual(self.run_sync(Server([full_media()]), "--zdroj", "anilist"), 0)
                al_files = {p: p.read_bytes() for p in self.root.rglob("*")
                            if p.is_file() and ("anilist" in p.parts or p.name == "anilist-state.json")}

        with patch.object(State, "save", save):
            self.assertEqual(self.run_sync(Server(), "--zdroj", "tenrai"), 0)
        self.assertTrue(interleaved)
        for path, content in al_files.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertTrue(State(self.root).data["list_complete"])
        self.assertTrue(State(self.root, "anilist").data["anilist"]["snapshot"]["done"])

    def test_cteni_ciziho_neuplneho_bloku_a_vymeny(self):
        foreign, local = Storage(self.root, "tenrai"), Storage(self.root, "anilist")
        foreign.put("tenrai", "list", 1, {"status": "Currently Airing"})
        foreign.flush()
        self.assertEqual(osobni.neighborhood(local), {1})
        path = foreign.path(("tenrai", "list", 0))
        path.write_bytes(b"nedokonceny gzip")
        self.assertEqual(osobni.neighborhood(local), set())
        foreign.dirty.add(("tenrai", "list", 0))
        foreign.flush()
        self.assertEqual(osobni.neighborhood(local), {1})
        path.unlink()
        self.assertEqual(osobni.neighborhood(local), set())

    def test_stav_se_ulozi_pred_chybou_okoli_a_meta(self):
        for method in ("nearby", "write_meta"):
            with self.subTest(misto=method), patch.object(AniList, method, side_effect=ValueError("TAJNY_TITUL")):
                self.assertEqual(self.run_sync(Server([full_media()]), "--zdroj", "anilist"), 1)
                self.assertTrue((self.root / "anilist-state.json").exists())
                self.assertTrue(State(self.root, "anilist").get("anilist", 1)[0])
                self.assertIsNotNone(Storage(self.root).get("anilist", "media", 1))

    def test_okoli_jednou_a_po_zmene(self):
        runner = self.runner(Server())
        writer = Storage(self.root, "tenrai")
        with patch("anilist.neighborhood", wraps=osobni.neighborhood) as compute:
            runner.nearby()
            runner.storage.put("anilist", "media", 1, compact(full_media()))
            runner.nearby()
            runner.nearby()
            self.assertEqual(compute.call_count, 1)
            writer.put("tenrai", "list", 1, {"status": "Currently Airing"})
            writer.flush()
            self.assertEqual(runner.nearby(), {1})
            self.assertEqual(compute.call_count, 2)

    def test_anilist_nema_devadesatiminutovy_strop(self):
        server = Server([media(i) for i in range(1, 110)])
        options = sync.parser().parse_args(["--data", str(self.root), "--zdroj", "anilist", "--minutes", "200",
                                            "--anilist-rate", "0.1", "--max-dotazu", "12", "--dnes", "2026-11-03"])
        self.assertEqual(sync.run(options, server, self.clock.now, self.clock.sleep, self.clock.utcnow,
                                  log=lambda line: None), 0)
        self.assertGreater(self.clock.now(), 90 * 60)
        self.assertEqual(len(server.calls), 12)

    def test_vzorek_stranky_idmal_dostahovani_a_export(self):
        rows = [full_media(1, 1101), full_media(2, 1102)]
        server = ConnectionsServer(rows)
        rows[0]["staff"]["pageInfo"]["hasNextPage"] = True
        server.pages[(1, "staff", 2)] = {"pageInfo": {"hasNextPage": False}, "edges": [staff(2)]}
        self.assertEqual(self.run_sync(server, "--vzorek", "26"), 0)
        get_calls = [url for url, payload in server.calls if payload is None and "?" in url]
        self.assertEqual([parse_qs(urlsplit(url).query) for url in get_calls],
                         [{"page": ["1"], "order_by": ["mal_id"], "sort": ["asc"]},
                          {"page": ["2"], "order_by": ["mal_id"], "sort": ["asc"]}])
        queries = [payload for url, payload in server.calls if payload]
        self.assertFalse(any("popularity_greater" in payload["query"] or "UPDATED_AT_DESC" in payload["query"]
                             for payload in queries))
        batches = [payload for payload in queries if "idMal_in" in payload["query"]]
        self.assertEqual(batches[0]["variables"]["ids"], [1101, 1102])
        self.assertTrue(any("staff(page:2" in payload["query"] for payload in queries))
        meta = build_web.build(self.root, self.today, log=lambda line: None)
        self.assertEqual(meta["anilist"]["pocet"], 2)
        exported = json.loads((self.root / "web/al_detail/0011.json").read_text(encoding="utf-8"))
        self.assertEqual(set(exported), {"1101", "1102"})
        self.assertEqual(exported["1101"]["al"], 1)
        people = json.loads((self.root / "web/al_lide/0011.json").read_text(encoding="utf-8"))
        self.assertEqual([row[0] for row in people["1101"]["staff"]], [1, 2])
        self.assertEqual(people["1102"], {key: compact(rows[1])[key] for key in ("staff", "characters")})
        self.assertEqual(exported["1102"], {**{key: value for key, value in compact(rows[1]).items()
                                                if key not in build_web.AL_EXCLUDE}, "al": 2})
        before = {path.relative_to(self.root / "web"): path.read_bytes() for path in (self.root / "web").rglob("*.json")}
        build_web.build(self.root, self.today, log=lambda line: None)
        self.assertEqual(before, {path.relative_to(self.root / "web"): path.read_bytes()
                                 for path in (self.root / "web").rglob("*.json")})
        for name, size in meta["velikosti"].items():
            self.assertEqual((self.root / "web" / name).stat().st_size, size[0])

    def test_vzorek_jen_vybrane_stranky_i_pri_starsich_datech(self):
        state, storage = State(self.root), Storage(self.root)
        storage.put("tenrai", "list", 9999, {"status": "Currently Airing"})
        storage.flush()
        server = ConnectionsServer([full_media()])
        self.assertEqual(self.run_sync(server, "--zdroj", "tenrai", "--vzorek", "1"), 0)
        self.assertFalse(any("/9999/" in url or "sort=desc" in url or "status=" in url for url, _ in server.calls))
        self.assertFalse((self.root / "anilist-state.json").exists())
        tenrai_files = {p: p.read_bytes() for p in self.root.rglob("*")
                        if p.is_file() and ("tenrai" in p.parts or p.name in ("state.json", "tenrai-den.json"))}
        server.calls.clear()
        self.assertEqual(self.run_sync(server, "--zdroj", "anilist", "--vzorek", "1"), 0)
        self.assertTrue(all(payload is not None for _, payload in server.calls))
        batches = [payload for _, payload in server.calls if "idMal_in" in payload["query"]]
        self.assertEqual(batches[0]["variables"]["ids"], [1101, 1102])
        for path, content in tenrai_files.items():
            self.assertEqual(path.read_bytes(), content)

    def test_export_al_respektuje_okoli_a_aktivni_katalog(self):
        storage = Storage(self.root)
        for ident in (1101, 1102):
            storage.put("tenrai", "list", ident, {"status": "Finished Airing"})
        for ident, mal in ((1, 1101), (2, 1102), (3, 1103)):
            storage.put("anilist", "media", ident, compact(full_media(ident, mal)))
        storage.flush()
        al_state = State(self.root, "anilist")
        al_state.data["anilist"]["neighborhood"] = [1102]
        al_state.save()
        build_web.build(self.root, date(2026, 11, 11), "okoli", log=lambda line: None)
        result = json.loads((self.root / "web/al_detail/0011.json").read_text(encoding="utf-8"))
        self.assertEqual(set(result), {"1102"})
        build_web.build(self.root, date(2026, 11, 11), "cele", log=lambda line: None)
        result = json.loads((self.root / "web/al_detail/0011.json").read_text(encoding="utf-8"))
        self.assertEqual(set(result), {"1101", "1102"})

    def test_necekana_vyjimka_vypise_typ_a_nase_misto_bez_textu(self):
        lines = []
        options = sync.parser().parse_args(["--data", str(self.root), "--zdroj", "tenrai"])
        with patch.object(Tenrai, "run", side_effect=RuntimeError("TAJNY_TITUL")):
            self.assertEqual(sync.run(options, Server(), self.clock.now, self.clock.sleep,
                                      self.clock.utcnow, log=lines.append), 1)
        output = "\n".join(lines)
        self.assertNotIn("TAJNY_TITUL", output)
        self.assertIn("RuntimeError", output)
        self.assertRegex(output, r"sync\.py:\d+:run")
        self.assertNotIn("unittest", output)


if __name__ == "__main__":
    unittest.main()

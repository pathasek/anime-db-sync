"""Úplnost řezů a obnova rozpracovaného AniListu."""

from datetime import date, timedelta
from pathlib import Path
from tests.helpers import TemporaryDirectory
import unittest

from anilist import AniList, compact, new_slice
from state import State
from tempo import Client, Limiter, Response, StopSource
from tests.helpers import Clock, Server, fixture, media
from uloziste import Storage


class AniListTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state, self.storage, self.clock = State(self.temp.name, "anilist"), Storage(self.temp.name), Clock()
        self.today = date(2026, 11, 3)

    def runner(self, server, today=None, deadline=float("inf")):
        client = Client("anilist", Limiter(20, self.clock.now, self.clock.sleep), server, log=lambda line: None)
        return AniList(client, self.state, self.storage, today or self.today, deadline,
                       wall_now=lambda: 1000, log=lambda line: None)

    def test_tvar_zhuštění(self):
        result = compact(fixture("anilist")["data"]["Page"]["media"][0])
        self.assertEqual(result["tags"], [[5101, 85, 1]])
        self.assertEqual(result["recommendations"], [[4102, 1102, 7]])
        self.assertEqual(result["relations"], [["SEQUEL", 4102, 1102, "ANIME", "MOVIE"]])

    def test_male_rezy_vcetne_nuly_dospelych_a_lziveho_total(self):
        rows = [media(i + 1, pop, adult) for i, (pop, adult) in enumerate(
            [(0, False), (1, False), (9, False), (10, False), (49, False), (50, True),
             (199, False), (200, False), (999, True), (1000, False), (4999, False),
             (5000, True), (19999, False), (20000, False), (99999, True), (100000, True)])]
        server = Server(rows)
        runner = self.runner(server)
        runner.run()
        self.assertTrue(runner.snapshot["done"])
        self.assertEqual(len(dict(self.storage.items("anilist", "media"))), len(rows))
        self.assertEqual(runner.snapshot["difference"], 0)
        self.assertEqual(server.tags_calls, 1)
        runner.run()
        self.assertEqual(server.tags_calls, 1)

    def test_otevreny_rez_neposila_horni_mez(self):
        server = Server([media(1, 150000), media(2, 99999)])
        runner = self.runner(server)
        runner.run_snapshot()
        self.assertEqual(len(dict(self.storage.items("anilist", "media"))), 2)
        queries = [payload["variables"] for url, payload in server.calls if "popularity_greater" in payload["query"]]
        self.assertTrue(any(query["greater"] == 99999 and "lesser" not in query for query in queries))

    def test_nad_5000_setkani_uprostred(self):
        rows = [media(i, 20) for i in range(1, 6102)]
        server = Server(rows)
        runner = self.runner(server)
        runner.run_snapshot()
        self.assertEqual(runner.snapshot["count"], len(rows))
        self.assertEqual(runner.snapshot["difference"], 0)
        queries = [payload for url, payload in server.calls if "popularity_greater" in payload["query"]]
        self.assertTrue(any("ID_DESC" in query["query"] for query in queries))
        self.assertTrue(all(query["variables"]["page"] <= 100 for query in queries))
        self.assertEqual(len(runner.snapshot["ids"]), len(set(runner.snapshot["ids"])))

    def test_nad_10000_rozdeleni(self):
        rows = [media(i, 20 if i % 2 else 30) for i in range(1, 11012)]
        server = Server(rows)
        runner = self.runner(server)
        runner.run_snapshot()
        self.assertTrue(runner.snapshot["done"])
        self.assertEqual(runner.snapshot["count"], len(rows))
        self.assertEqual(runner.snapshot["incomplete"], [])
        queries = [payload for url, payload in server.calls if "popularity_greater" in payload["query"]]
        self.assertTrue(any(query["variables"]["greater"] == 29 for query in queries))
        self.assertTrue(all(query["variables"]["page"] <= 100 for query in queries))

    def test_jedina_popularita_neuplny_rez_bez_slepeho_hledani(self):
        rows = [media(i, 0) for i in range(1, 10052)]
        server = Server(rows)
        runner = self.runner(server)
        runner.run_snapshot()
        self.assertTrue(runner.snapshot["done"])
        self.assertEqual(runner.snapshot["count"], 10000)
        self.assertEqual(runner.snapshot["difference"], 51)
        self.assertEqual(len(runner.snapshot["incomplete"]), 1)

    def test_navazani_stranky_po_padu(self):
        server = Server([media(i) for i in range(1, 550)])
        server.fail_at = 7
        runner = self.runner(server)
        with self.assertRaises(RuntimeError):
            runner.run_snapshot()
        self.storage.flush()
        self.state.save()
        saved_part = dict(runner.snapshot["todo"][0])
        ids = set(runner.snapshot["ids"])
        self.state, self.storage = State(self.temp.name, "anilist"), Storage(self.temp.name)
        resumed_server = Server(server.rows)
        resumed = self.runner(resumed_server)
        resumed.run_snapshot()
        first_page = next(payload for url, payload in resumed_server.calls if "popularity_greater" in payload["query"])
        self.assertEqual(first_page["variables"]["page"], saved_part["page"])
        self.assertTrue(ids.issubset(set(resumed.snapshot["ids"])))
        self.assertEqual(resumed.snapshot["count"], 549)

    def test_casovy_strop_uchova_rez(self):
        runner = self.runner(Server([media(i) for i in range(1, 600)]), deadline=12)
        with self.assertRaises(StopSource):
            runner.run_snapshot()
        self.assertFalse(runner.snapshot["done"])
        self.assertTrue(runner.snapshot["todo"])
        self.assertLessEqual(self.clock.now(), 12)

    def test_doporuceni_aliasy_404_rozpad_a_vice_stran(self):
        server = Server()
        server.alias_fail = True
        server.missing = {3}
        runner = self.runner(server)
        for ident in (1, 2, 3):
            row = media(ident)
            row["recommendations"]["pageInfo"]["hasNextPage"] = True
            runner.save_media(row)
        server.rec_pages[(1, 2)] = {"pageInfo": {"hasNextPage": True}, "nodes": [
            {"rating": 8, "mediaRecommendation": {"id": 8, "idMal": 108, "type": "ANIME"}},
            {"rating": 4, "mediaRecommendation": {"id": 9, "idMal": None, "type": "MANGA"}}]}
        server.rec_pages[(1, 3)] = {"pageInfo": {"hasNextPage": False}, "nodes": [
            {"rating": 3, "mediaRecommendation": {"id": 10, "idMal": 110, "type": "ANIME"}}]}
        runner.finish_recommendations()
        self.assertEqual(runner.al["pending"], {})
        self.assertEqual(self.storage.get("anilist", "media", 1)["recommendations"], [[8, 108, 8], [10, 110, 3]])
        self.assertTrue(any("a1: Media" in payload["query"] for url, payload in server.calls))
        self.assertEqual(self.state.get("al_recs", 3)[3], 1)

    def test_od_konce_svoleni_jen_okoli_a_zmeny_do_posledniho_behu(self):
        rows = [media(i) for i in range(1, 61)]
        rows[0]["idMal"] = 1101
        rows[1]["idMal"] = 1102
        self.storage.put("tenrai", "list", 1101, {"status": "Currently Airing"})
        self.storage.put("tenrai", "list", 1102, {"status": "Currently Airing"})
        for row in rows:
            row["updatedAt"] = 200
        rows[-1]["updatedAt"] = 50
        server = Server(rows)
        server.updated = rows
        self.state.data["anilist"]["last_run"] = 100
        for ident in (1101, 1102):
            self.state.success("al_mal", ident, date(2026, 11, 10))
        runner = self.runner(server, date(2026, 11, 11))
        runner.run()
        queries = [payload for url, payload in server.calls]
        self.assertFalse(any("popularity_greater" in query["query"] for query in queries))
        updates = [query for query in queries if "UPDATED_AT_DESC" in query["query"]]
        self.assertEqual(len(updates), 2)
        nearby_queries = [query for query in queries if "idMal_in" in query["query"]]
        self.assertEqual(nearby_queries[0]["variables"]["ids"], [1101, 1102])
        self.assertEqual(runner.al["last_run"], 1000)
        self.assertEqual(runner.al["changed"], [])
        self.assertEqual(server.tags_calls, 1)
        meta = self.storage.read_json("anilist", "meta")
        self.assertEqual(meta["pocet"], 2)
        self.assertEqual(meta["datum"], "2026-11-11")

    def test_chybejici_idmal_se_neopakuje_tricet_dni(self):
        self.storage.put("tenrai", "list", 1101, {"status": "Currently Airing"})
        server = Server()
        runner = self.runner(server, date(2026, 11, 11))
        runner.run()
        count = sum("idMal_in" in payload["query"] for url, payload in server.calls)
        runner.run()
        self.assertEqual(sum("idMal_in" in payload["query"] for url, payload in server.calls), count)

    def test_po_svoleni_se_stara_doporuceni_mimo_okoli_nestahuji(self):
        runner = self.runner(Server(), date(2026, 11, 11))
        for ident in (1, 2):
            row = media(ident)
            row["recommendations"]["pageInfo"]["hasNextPage"] = True
            runner.save_media(row)
        self.storage.put("tenrai", "list", 100001, {"status": "Currently Airing"})
        server = Server()
        runner = self.runner(server, date(2026, 11, 11))
        runner.run()
        aliases = [payload["query"] for url, payload in server.calls if "a0: Media" in payload["query"]]
        self.assertEqual(len(aliases), 1)
        self.assertIn("Media(id:1)", aliases[0])
        self.assertNotIn("Media(id:2)", aliases[0])
        self.assertEqual(runner.al["pending"], {"2": {"recommendations": 2}})

    def test_svoleni_skonci_i_uprostred_behu(self):
        server = Server([media(i) for i in range(1, 120)])
        runner = self.runner(server)
        runner.today_fn = lambda: date(2026, 11, 11) if len(server.calls) >= 2 else date(2026, 11, 10)
        with self.assertRaises(StopSource):
            runner.run_snapshot()
        self.assertEqual(len(server.calls), 2)
        self.assertFalse(runner.snapshot["done"])

    def test_zmeny_nad_5000_overi_jen_zname_okoli_a_posune_cas(self):
        rows = [media(i) for i in range(1, 5102)]
        for row in rows:
            row["updatedAt"] = 200
        self.storage.put("tenrai", "list", 105101, {"status": "Currently Airing"})
        self.state.data["anilist"]["last_run"] = 100
        self.state.success("al_mal", 105101, date(2026, 11, 10))
        server = Server(rows)
        runner = self.runner(server, date(2026, 11, 11))
        runner.run()
        self.assertEqual(runner.al["last_run"], 1000)
        self.assertEqual(self.storage.get("anilist", "media", 5101)["idMal"], 105101)
        payloads = [payload for url, payload in server.calls]
        self.assertFalse(any(payload["variables"].get("page", 0) > 100 for payload in payloads))
        extra = [payload for payload in payloads if "idMal_in" in payload["query"]]
        self.assertEqual(len(extra), 2)
        self.assertTrue(all(payload["variables"]["ids"] == [105101] for payload in extra))

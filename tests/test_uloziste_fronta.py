"""Úložiště, zhuštění a pořadí nočního stahování."""

from datetime import date, timedelta
import gzip
import json
from pathlib import Path
from tests.helpers import TemporaryDirectory
import unittest

from fronta import Queue, Tenrai
from osobni import neighborhood
from state import State, json_bytes
from tempo import Client, Limiter
import tenrai
from tests.helpers import Clock, Server, fixture
from uloziste import Storage


class StorageQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage(self.temp.name)
        self.state = State(self.temp.name)
        self.today = date(2026, 11, 3)

    def catalog(self):
        for row in fixture("tenrai")["data"]:
            self.storage.put("tenrai", "list", row["mal_id"], tenrai.compact(row))

    def test_deterministicky_gzip_a_neprepsany_soubor(self):
        self.catalog()
        self.storage.flush()
        path = self.storage.path(("tenrai", "list", 1))
        first, timestamp = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(first[4:8], b"\0" * 4)
        self.assertFalse(first[3] & 8)
        rows = [json.loads(line) for line in gzip.decompress(first).splitlines()]
        self.assertEqual([row["id"] for row in rows], [1101, 1102])
        self.catalog()
        self.storage.dirty.add(("tenrai", "list", 1))
        self.storage.flush()
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(path.stat().st_mtime_ns, timestamp)
        self.assertEqual(Storage(self.temp.name).get("tenrai", "list", 1101), rows[0]["d"])

    def test_liny_blok_a_limit_velikosti(self):
        from unittest.mock import patch
        self.catalog()
        self.storage.flush()
        other = Storage(self.temp.name)
        self.assertEqual(other.cache, {})
        other.get("tenrai", "list", 1101)
        self.assertEqual(len(other.cache), 1)
        other.put("tenrai", "list", 1103, {"title": "Testovací anime 3"})
        with patch("uloziste.MAX_BYTES", 1), self.assertRaises(ValueError):
            other.flush()
        self.assertEqual(len(other.dirty), 1)

    def test_zhuštění(self):
        row = tenrai.compact(fixture("tenrai")["data"][0])
        self.assertEqual(row["img"], "https://priklad.invalid/obrazky/1.jpg")
        self.assertNotIn("url", row)
        self.assertEqual(row["trailer"], {"youtube_id": "vymyslene1"})
        self.assertIn("background", row)
        self.assertIn("broadcast", row)
        self.assertNotIn("url", row["genres"][0])
        self.assertEqual(tenrai.compact({"images": {"jpg": {"image_url": "https://cdn.myanimelist.net/vymyslena/1.jpg"}}}),
                         {"img": "/vymyslena/1.jpg"})
        details = fixture("details")
        self.assertEqual(tenrai.reduce("recs", details["recs"]["data"]), [[1102, 42]])
        self.assertEqual(set(tenrai.reduce("full", details["full"]["data"])),
                         {"relations", "theme", "external", "streaming"})
        self.assertEqual(tenrai.reduce("stats", details["stats"]["data"]), details["stats"]["data"])

    def test_prvni_poradi_od_members_a_rotace(self):
        self.catalog()
        queue = Queue(self.state, self.storage, self.today)
        initial = list(queue.initial())
        self.assertEqual(initial[:4], [("recs", 1102), ("recs", 1101), ("full", 1102), ("full", 1101)])
        self.assertEqual(list(queue.rotation()), [])
        self.state.data["list_complete"] = True
        self.assertFalse(queue.first_done())
        for endpoint, ident in initial:
            self.state.success(endpoint, ident, self.today - timedelta(days=95))
        self.state.success("stats", 1102, self.today - timedelta(days=100))
        self.assertTrue(queue.first_done())
        self.assertEqual(next(queue.rotation()), ("stats", 1102))
        self.state.failure("staff", 1102, self.today, 404)
        self.assertNotIn(("stats", 1102), list(queue.rotation()))

    def test_prvni_noc_seznam_pred_doplnky(self):
        clock, server = Clock(), Server()
        client = Client("tenrai", Limiter(108, clock.now, clock.sleep), server, log=lambda line: None)
        Tenrai(client, self.state, self.storage, self.today, float("inf")).run()
        urls = [url for url, payload in server.calls]
        self.assertIn("sort=asc", urls[0])
        self.assertFalse(any("sort=desc" in url or "status=" in url for url in urls))
        asc = next(index for index, url in enumerate(urls) if "sort=asc" in url)
        rec2 = next(index for index, url in enumerate(urls) if "/1102/recommendations" in url)
        self.assertLess(asc, rec2)
        self.assertTrue(Queue(self.state, self.storage, self.today).first_done())

    def test_rotace_seznamu_a_doplnku_spolecne_od_nejstarsiho(self):
        self.catalog()
        queue = Queue(self.state, self.storage, self.today)
        for endpoint, ident in list(queue.initial()):
            self.state.success(endpoint, ident, self.today)
        self.state.data.update(list_complete=True, list_pages=1)
        self.state.success("list", "asc:1", self.today - timedelta(days=8), pages=True)
        self.state.success("full", 1101, self.today - timedelta(days=100))
        self.assertEqual(list(queue.rotation()), [("full", 1101), ("list", 1)])

    def test_400_ukonci_strankovani_a_404_ukonci_anime(self):
        from tempo import Response
        clock = Clock()
        calls = []

        def send(method, url, *args):
            calls.append(url)
            return Response(400 if "?" in url else 404)

        client = Client("tenrai", Limiter(108, clock.now, clock.sleep), send, log=lambda line: None)
        job = Tenrai(client, self.state, self.storage, self.today, float("inf"))
        job.initial_list()
        self.assertTrue(self.state.data["list_complete"])
        self.assertEqual(len(calls), 1)
        job.fetch_item("full", 1101)
        job.fetch_item("chars", 1101)
        self.assertEqual(len(calls), 2)
        self.assertTrue(self.state.gone(1101))

    def test_429_ukonceny_dotaz_zapise_chybnou_noc(self):
        from tempo import Response, StopSource
        clock = Clock()
        client = Client("tenrai", Limiter(108, clock.now, clock.sleep),
                        lambda *args: Response(429), log=lambda line: None)
        job = Tenrai(client, self.state, self.storage, self.today, float("inf"))
        with self.assertRaises(StopSource):
            job.fetch_item("stats", 1101)
        self.assertEqual(self.state.get("stats", 1101)[1], 1)

    def test_okoli_ptw_sousede_a_nejednoznacne_tituly(self):
        self.catalog()
        personal = Path(self.temp.name) / "osobni"
        personal.mkdir()
        (personal / "anime_list.json").write_bytes(json_bytes([{"mal_url": "https://myanimelist.net/anime/1102/test"}]))
        (personal / "plan_to_watch.json").write_bytes(json_bytes([{"name": "VYMYŠLENÝ TITUL-1"}]))
        self.storage.put("tenrai", "recs", 1102, [[1200 + i, 100 - i] for i in range(12)])
        nearby = neighborhood(self.storage, personal, self.state)
        self.assertEqual(nearby, {1101, 1102, *range(1200, 1210)})
        self.assertEqual(neighborhood(self.storage, None, self.state), {1101})
        self.storage.put("tenrai", "list", 1103, {"title": "Vymyšlený titul 1"})
        (personal / "anime_list.json").unlink()
        self.assertEqual(neighborhood(self.storage, personal, self.state), {1101})
        self.state.failure("list", 1101, self.today, 404)
        self.assertEqual(neighborhood(self.storage, personal, self.state), {1103})

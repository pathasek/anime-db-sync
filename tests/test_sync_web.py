"""Pády, obnovení, bezpečnost logů a deterministický export."""

import contextlib
from datetime import date
import io
import gzip
import json
from pathlib import Path
import signal
from tests.helpers import TemporaryDirectory
import unittest
from unittest.mock import patch

import anilist
import build_web
from state import State
import sync
import tenrai
from tempo import Response
from tests.helpers import Clock, Server, fixture
from uloziste import Storage


class SyncWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def args(self, *extra):
        return sync.parser().parse_args(["--data", self.temp.name, "--dnes", "2026-11-11", *extra])

    def run_sync(self, server, args=None):
        return sync.run(args or self.args(), send=server, now=self.clock.now, sleep=self.clock.sleep,
                        utcnow=self.clock.utcnow, log=lambda line: None)

    def test_pad_ulozi_hotove_a_druhy_beh_je_neopakuje(self):
        server = Server()
        server.fail_at = 8
        self.assertEqual(self.run_sync(server), 1)
        state = State(self.temp.name)
        self.assertTrue(state.get("recs", 1101)[0])
        self.assertEqual(Storage(self.temp.name).get("tenrai", "recs", 1101), [[1102, 42]])
        repeated = Server()
        self.assertEqual(self.run_sync(repeated), 0)
        self.assertFalse(any("/1101/recommendations" in url for url, payload in repeated.calls))
        self.assertFalse(any("/1101/statistics" in url for url, payload in repeated.calls))
        self.assertTrue(State(self.temp.name).data["list_complete"])

    def test_anilist_ma_prednost_a_max_dotazu_pocita_pokusy(self):
        server = Server()
        self.assertEqual(self.run_sync(server, self.args("--max-dotazu", "4")), 0)
        self.assertEqual(len(server.calls), 4)
        self.assertTrue(server.calls[0][0].startswith("https://graphql.anilist.co"))
        self.assertTrue(server.calls[1][0].startswith("https://graphql.anilist.co"))
        self.assertTrue(server.calls[2][0].startswith("https://api.tenrai.org"))
        state = State(self.temp.name)
        self.assertEqual(sum(state.data["tenrai_days"].values()), 2)

    def test_429_jednoho_zdroje_neukonci_druhy(self):
        server = Server()

        def send(method, url, *args):
            if "anilist.co" in url:
                return Response(429)
            return server(method, url, *args)

        self.assertEqual(self.run_sync(send), 0)
        self.assertTrue(any("api.tenrai.org" in url for url, payload in server.calls))
        self.assertEqual(self.clock.sleeps[:2], [30, 60])

    def test_konec_casu_ctrl_c_sigterm_vzdy_ulozi(self):
        self.assertEqual(self.run_sync(Server(), self.args("--minutes", "0")), 0)
        self.assertTrue((self.root / "state.json").exists())
        for failure in (KeyboardInterrupt, None):
            server = Server()

            def send(*args):
                if failure:
                    raise failure()
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

            self.assertEqual(self.run_sync(send), 0)
            self.assertTrue((self.root / "raw" / "anilist" / "meta.json").exists())

    def test_checkpoint_po_500_dotazech_a_na_konci(self):
        server = Server()
        server.known_pages = [{"mal_id": 1000 + ident, "title": f"Testovací anime {ident}",
                               "members": ident, "status": "Finished Airing"} for ident in range(1, 130)]
        saved = []
        original = State.save

        def save(state):
            if state.source == "tenrai":
                saved.append(sum(state.data["tenrai_days"].values()))
            original(state)

        with patch.object(State, "save", save):
            self.assertEqual(self.run_sync(server, self.args("--max-dotazu", "501")), 0)
        self.assertIn(498, saved)
        self.assertEqual(saved[-1], 499)
        self.assertEqual(saved, sorted(saved))
        self.assertEqual(len(server.calls), 501)

    def test_snimku_nedovoli_start_po_pulnoci_konce_svoleni(self):
        from datetime import datetime, timezone
        self.clock.epoch = datetime(2026, 11, 10, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        args = self.args("--max-dotazu", "5")
        args.dnes = None
        server = Server()
        self.assertEqual(self.run_sync(server, args), 0)
        al_calls = [payload for url, payload in server.calls if "anilist.co" in url]
        self.assertEqual(len(al_calls), 1)

    def populate(self):
        storage, state = Storage(self.temp.name), State(self.temp.name)
        for row in fixture("tenrai")["data"]:
            storage.put("tenrai", "list", row["mal_id"], tenrai.compact(row))
            state.success("list", row["mal_id"], date(2026, 11, 3))
        for endpoint, response in fixture("details").items():
            storage.put("tenrai", endpoint, 1101, tenrai.reduce(endpoint, response["data"]))
            state.success(endpoint, 1101, date(2026, 11, 3))
        media = anilist.compact(fixture("anilist")["data"]["Page"]["media"][0])
        storage.put("anilist", "media", media["id"], media)
        storage.write_json("anilist", "tagy", {"5101": {"id": 5101, "name": "Vymyšlený tag",
                            "category": "Test", "isAdult": False, "description": "Vymyšlený popis."}})
        al_state = State(self.temp.name, "anilist")
        al_state.data["anilist"]["snapshot"] = {"done": True, "count": 1, "total": 1, "date": "2026-11-03"}
        al_state.save()
        storage.flush()
        state.save()
        return storage, state

    def read_web(self, name):
        return json.loads((self.root / "web" / name).read_text(encoding="utf-8"))

    def test_tvar_vsech_souboru_a_determinismus(self):
        self.populate()
        day = date(2026, 11, 3)
        meta = build_web.build(self.temp.name, day, log=lambda line: None)
        self.assertEqual(set(meta["uplnost"]), {"list", "recs", "full", "staff", "chars", "stats", "anilist"})
        self.assertEqual(meta["uplnost"]["stats"], 0.5)
        core = self.read_web("core.json")
        self.assertEqual(core["data"]["id"], [1101, 1102])
        self.assertEqual(core["data"]["delka_min"], [24, 113])
        self.assertEqual(self.read_web("dict.json")["genres"]["710"], "Testovací žánr")
        self.assertEqual(self.read_web("dict.json")["tags"]["5101"][0], "Vymyšlený tag")
        self.assertEqual(self.read_web("recs.json"), {"mal": [1101, 1102, 42], "al": [1101, 1102, 7]})
        self.assertEqual(self.read_web("syn/0002.json")["1101"], "Vymyšlená synopse o hledání ztracené tužky.")
        self.assertEqual(self.read_web("staff.json")["anime"]["1101"], [[2101, 0]])
        self.assertEqual(self.read_web("chars/0001.json")["anime"]["1101"], [[3101, 1, 3, 2102, 0]])
        self.assertEqual(self.read_web("stats.json")["1101"][-1], [0, 0, 0, 0, 0, 0, 0, 12, 0, 0])
        self.assertEqual(self.read_web("rel.json"), {"1101": [["Sequel", [1102]]]})
        self.assertEqual(self.read_web("al_core.json")["data"]["tagy"], [[5101, 85, 1]])
        before = {str(path.relative_to(self.root / "web")): path.read_bytes()
                  for path in (self.root / "web").rglob("*.json")}
        (self.root / "web" / "stare.json").write_text("{}")
        build_web.build(self.temp.name, day, log=lambda line: None)
        after = {str(path.relative_to(self.root / "web")): path.read_bytes()
                 for path in (self.root / "web").rglob("*.json")}
        self.assertEqual(before, after)
        for name, size in meta["velikosti"].items():
            content = (self.root / "web" / name).read_bytes()
            self.assertEqual([len(content), len(gzip.compress(content, compresslevel=9, mtime=0))], size, name)

    def test_pryc_vynechane_vsude_a_vychozi_cele_po_datu(self):
        storage, state = self.populate()
        state.failure("stats", 1102, date(2026, 11, 11), 404)
        state.save()
        meta = build_web.build(self.temp.name, date(2026, 11, 11), log=lambda line: None)
        self.assertEqual(meta["al_tagy"], "cele")
        self.assertEqual(meta["pryc"], 1)
        self.assertEqual(meta["pocet"], 1)
        self.assertEqual(self.read_web("recs.json"), {"mal": [], "al": []})
        self.assertEqual(self.read_web("rel.json"), {"1101": []})
        self.assertNotIn("1102", self.read_web("syn/0002.json"))
        row = storage.get("tenrai", "list", 1101)
        row["status"] = "Finished Airing"
        storage.put("tenrai", "list", 1101, row)
        # Při změně záznamu v paměti je potřeba jej výslovně označit jako změněný.
        storage.dirty.add(("tenrai", "list", 1))
        storage.flush()
        build_web.build(self.temp.name, date(2026, 11, 11), "okoli", log=lambda line: None)
        self.assertEqual(self.read_web("al_core.json")["pocet"], 0)
        build_web.build(self.temp.name, date(2026, 11, 11), "cele", log=lambda line: None)
        self.assertEqual(self.read_web("al_core.json")["data"]["id"], [1101])

    def test_delka_a_prazdny_export(self):
        self.assertEqual(build_web.duration_minutes("30 sec"), 0.5)
        self.assertEqual(build_web.duration_minutes("1 hr 53 min"), 113)
        self.assertIsNone(build_web.duration_minutes("Unknown"))
        self.assertIsNone(build_web.day_only(None))
        meta = build_web.build(self.temp.name, date(2026, 11, 3), log=lambda line: None)
        self.assertEqual(meta["pocet"], 0)

    def test_logy_nikdy_neobsahuji_data_ani_vyjimku(self):
        server = Server()
        server.known_pages[0]["title"] = "TAJNY_TITUL"
        server.known_pages[0]["synopsis"] = "TAJNY_TITUL"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sync.run(self.args(), server, self.clock.now, self.clock.sleep, self.clock.utcnow)
            build_web.build(self.temp.name, date(2026, 11, 11))
            with patch("build_web.build", side_effect=ValueError("TAJNY_TITUL")):
                self.assertEqual(build_web.main(["--data", self.temp.name]), 1)
            failing = Server()
            failing.fail_at = 1
            self.assertEqual(sync.run(self.args(), failing, self.clock.now, self.clock.sleep, self.clock.utcnow), 1)
            with patch("sync.run", side_effect=ValueError("TAJNY_TITUL")):
                self.assertEqual(sync.main(["--data", self.temp.name]), 1)
        self.assertNotIn("TAJNY_TITUL", output.getvalue())
        self.assertNotIn("priklad.invalid", output.getvalue())


if __name__ == "__main__":
    unittest.main()

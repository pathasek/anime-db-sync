"""Čtečky novinek: stránkování, denní soubory, fronta, anomálie a celý průchod. Bez sítě."""

import copy
from datetime import date
import gzip
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import unittest

import build_web
import novinky
from novinky import Novinky
from state import State
import sync
from tempo import Client, Limiter, Response, StopSource
from tests.helpers import Clock, Server, TemporaryDirectory, fixture

TAJNE = "TAJNY_TEXT_RECENZE"
DNES = date(2026, 11, 8)


def radek_recenze(ident, anime, den, text=None, cas="12:00:00"):
    row = copy.deepcopy(fixture("novinky")["recenze"])
    row.update(mal_id=ident, date=f"{den}T{cas}+00:00", review=text or f"{TAJNE} {ident}")
    row["entry"]["mal_id"] = anime
    return row


def radek_posudku(par, den, text="Vymyšlený posudek."):
    row = copy.deepcopy(fixture("novinky")["posudek"])
    row.update(mal_id=par, date=f"{den}T00:00:00+00:00", content=text)
    return row


class Vypisy:
    """Falešný Tenrai se dvěma výpisy od nejnovějších, na stránce 4 řádky."""

    def __init__(self, recenze=(), posudky=None, velikost=4):
        if posudky is None:
            posudky = [radek_posudku("1101-1102", "2026-11-08")]
        self.recenze, self.posudky, self.velikost = list(recenze), list(posudky), velikost
        self.adresy = []
        self.vynutit = {}

    def __call__(self, method, url, headers, payload, timeout):
        self.adresy.append(url)
        parts = urlsplit(url)
        params = parse_qs(parts.query)
        feed = {"/v1/reviews/anime": ("recenze", self.recenze),
                "/v1/recommendations/anime": ("posudky", self.posudky)}[parts.path]
        if feed[0] == "recenze":
            assert params["spoilers"] == ["true"] and params["preliminary"] == ["true"]
        stranka = int(params["page"][0])
        if (feed[0], stranka) in self.vynutit:
            return self.vynutit[(feed[0], stranka)]
        rows = feed[1]
        start = (stranka - 1) * self.velikost
        return Response(200, {"pagination": {"last_visible_page": 9, "has_next_page": start + self.velikost < len(rows)},
                              "data": copy.deepcopy(rows[start:start + self.velikost])})

    def pocet(self, feed):
        return sum(1 for url in self.adresy if ("/reviews/" if feed == "recenze" else "/recommendations/") in url)


class NovinkyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.slozka = self.root / "texty"
        self.clock = Clock()
        self.logy = []

    def spust(self, server, cely=False, od=date(2026, 11, 7), dnes=DNES, smazano=None):
        state = State(self.root)
        limiter = Limiter(108, self.clock.now, self.clock.sleep, utcnow=self.clock.utcnow)
        klient = Client("tenrai", limiter, server, log=self.logy.append)
        beh = Novinky(klient, state, self.slozka, dnes, float("inf"), self.logy.append, cely=cely, od=od, smazano=smazano)
        uspech = beh.run()
        state.save()
        return uspech, state

    def cti(self, jmeno):
        with gzip.open(self.slozka / jmeno, "rt", encoding="utf-8") as stream:
            return [json.loads(line) for line in stream]

    def soubory(self):
        return sorted(path.name for path in self.slozka.glob("*")) if self.slozka.exists() else []

    def zakladni_recenze(self):
        # Od nejnovějších: 8. 11. (10, 9, 8), 7. 11. (7, 6, 5), 6. 11. (4, 3), 5. 11. (2, 1).
        dny = ["2026-11-08"] * 3 + ["2026-11-07"] * 3 + ["2026-11-06"] * 2 + ["2026-11-05"] * 2
        return [radek_recenze(10 - index, 1100 + index % 3, den) for index, den in enumerate(dny)]

    def test_prvni_beh_denni_soubory_tvar_zaznamu_a_fronta(self):
        server = Vypisy(self.zakladni_recenze(), [radek_posudku("1101-1102", "2026-11-08")])
        uspech, state = self.spust(server)
        self.assertTrue(uspech)
        # Hranice je 6. 11.: strany 1 a 2 jsou novější, třetí už celá starší.
        self.assertEqual(server.pocet("recenze"), 3)
        self.assertEqual(server.pocet("posudky"), 1)
        self.assertEqual(self.soubory(), ["fronta.json", "posudky-2026-11-08.jsonl.gz", "recenze-2026-11-06.jsonl.gz",
                                          "recenze-2026-11-07.jsonl.gz", "recenze-2026-11-08.jsonl.gz"])
        radek = self.cti("recenze-2026-11-08.jsonl.gz")[0]
        self.assertEqual(set(radek), {"a", "id", "d", "s", "t", "sp", "pr", "ep", "re", "x"})
        self.assertEqual((radek["id"], radek["d"], radek["re"], radek["ep"]), (8, "2026-11-08T12:00:00+00:00", 3, None))
        self.assertEqual([r["id"] for r in self.cti("recenze-2026-11-06.jsonl.gz")], [3, 4])
        self.assertEqual(self.cti("posudky-2026-11-08.jsonl.gz"),
                         [{"p": "1101-1102", "t": "Vymyšlený posudek.", "d": "2026-11-08"}])
        fronta = json.loads((self.slozka / "fronta.json").read_text(encoding="utf-8"))
        self.assertEqual(fronta["cekajici"], 3)
        self.assertEqual(fronta["anime"]["1101"], {"od": "2026-11-08", "posledni": "2026-11-08",
                                                  "recenze": 3, "posudky": 1, "zmena": 0})
        self.assertEqual(fronta["anime"]["1102"]["posudky"], 1)
        self.assertEqual(state.data["texty"]["recenze"]["posledni"], "2026-11-08")
        self.assertEqual((state.data["texty"]["recenze"]["nove"], state.data["texty"]["posudky"]["nove"]), (8, 1))
        self.assertFalse(any(TAJNE in line for line in self.logy))

    def test_druhy_beh_pridava_jen_nove_a_smazany_soubor_se_nevraci(self):
        recenze = self.zakladni_recenze()
        server = Vypisy(recenze)
        self.spust(server)
        stare = (self.slozka / "recenze-2026-11-08.jsonl.gz").read_bytes()
        server = Vypisy(recenze)
        self.assertTrue(self.spust(server)[0])
        self.assertEqual((self.slozka / "recenze-2026-11-08.jsonl.gz").read_bytes(), stare)
        nova = radek_recenze(11, 1101, "2026-11-08", cas="19:00:00")
        server = Vypisy([nova] + recenze)
        uspech, state = self.spust(server)
        self.assertEqual(state.data["texty"]["recenze"]["nove"], 1)
        self.assertEqual([r["id"] for r in self.cti("recenze-2026-11-08.jsonl.gz")], [8, 9, 10, 11])
        # Sloučený a smazaný soubor z 8. 11. se z překryvu nevytvoří znovu.
        (self.slozka / "recenze-2026-11-08.jsonl.gz").unlink()
        (self.slozka / "recenze-2026-11-07.jsonl.gz").unlink()
        self.spust(Vypisy([nova] + recenze))
        self.assertNotIn("recenze-2026-11-08.jsonl.gz", self.soubory())
        self.assertNotIn("recenze-2026-11-07.jsonl.gz", self.soubory())

    def test_duplicity_mezi_strankami_se_ulozi_jednou(self):
        rows = [radek_recenze(5, 1101, "2026-11-08"), radek_recenze(4, 1101, "2026-11-08"),
                radek_recenze(4, 1101, "2026-11-08"), radek_recenze(3, 1101, "2026-11-08"),
                radek_recenze(1, 1101, "2026-11-01")]
        self.spust(Vypisy(rows, velikost=2))
        self.assertEqual([r["id"] for r in self.cti("recenze-2026-11-08.jsonl.gz")], [3, 4, 5])

    def test_posudky_stejna_dvojice_vice_textu_a_prazdny_text(self):
        rows = [radek_posudku("1101-1102", "2026-11-08", "První."), radek_posudku("1101-1102", "2026-11-08", "Druhá."),
                radek_posudku("1101-1102", "2026-11-08", "První."), radek_posudku("5-6", "2026-11-08", None)]
        self.spust(Vypisy([radek_recenze(1, 1, "2026-11-08")], rows))
        ulozene = self.cti("posudky-2026-11-08.jsonl.gz")
        self.assertEqual(sorted((r["p"], r["t"]) for r in ulozene),
                         [("1101-1102", "Druhá."), ("1101-1102", "První."), ("5-6", None)])
        self.assertEqual(set(json.loads((self.slozka / "fronta.json").read_text(encoding="utf-8"))["anime"]),
                         {"1", "1101", "1102", "5", "6"})

    def test_404_za_prvni_strankou_je_konec_vypisu(self):
        server = Vypisy(self.zakladni_recenze())
        server.vynutit[("recenze", 2)] = Response(404)
        uspech, state = self.spust(server)
        self.assertTrue(uspech)
        self.assertEqual([r["id"] for r in self.cti("recenze-2026-11-08.jsonl.gz")], [8, 9, 10])
        self.assertEqual(state.data["texty"]["recenze"]["nove"], 4)

    def test_recenze_bez_data_jde_do_souboru_dne_behu(self):
        row = radek_recenze(77, 1101, "2026-11-08")
        row["date"] = None
        self.spust(Vypisy([row]))
        self.assertEqual(self.cti("recenze-2026-11-08.jsonl.gz")[0]["d"], None)

    def test_nic_noveho_nezapisuje_a_neposouva_posledni_datum(self):
        server = Vypisy([radek_recenze(1, 1101, "2026-11-01")], [radek_posudku("1-2", "2026-11-01")])
        uspech, state = self.spust(server)
        self.assertTrue(uspech)
        self.assertEqual(self.soubory(), [])
        self.assertEqual(state.data["texty"]["recenze"]["posledni"], "2026-11-07")
        self.assertEqual(state.data["texty"]["recenze"]["nove"], 0)

    def anomalie(self, uprav):
        """Vrátí složku po běhu, kde recenze skončily anomálií; staré soubory zůstanou beze změny."""
        self.slozka.mkdir(parents=True, exist_ok=True)
        stary = self.slozka / "recenze-2026-11-01.jsonl.gz"
        novinky.zapis_gz(stary, [{"id": 1, "x": "stará"}])
        pred = stary.read_bytes()
        server = Vypisy(self.zakladni_recenze(), [radek_posudku("1101-1102", "2026-11-08")])
        uprav(server)
        uspech, state = self.spust(server)
        self.assertFalse(uspech)
        self.assertEqual(stary.read_bytes(), pred)
        self.assertEqual(self.soubory().count("recenze-2026-11-08.jsonl.gz"), 0)
        self.assertNotIn("posledni", state.data["texty"]["recenze"])
        self.assertEqual(state.data["texty"]["recenze"]["chyba"]["den"], "2026-11-08")
        # Druhý feed jede dál nezávisle.
        self.assertEqual(self.cti("posudky-2026-11-08.jsonl.gz")[0]["p"], "1101-1102")
        self.assertFalse(any(TAJNE in line for line in self.logy))
        self.assertTrue(any("recenze: nečekaná odpověď" in line for line in self.logy))
        return state

    def test_anomalie_chybejici_pole(self):
        def uprav(server):
            del server.recenze[5]["review"]
        self.anomalie(uprav)

    def test_anomalie_jiny_tvar_odpovedi(self):
        for odpoved in (Response(200, {"data": {}, "pagination": {"has_next_page": True}}),
                        Response(200, {"data": [], "pagination": {}}),
                        Response(200, {"data": [], "pagination": {"has_next_page": False}}),
                        Response(200, {"data": [{"mal_id": "1"}], "pagination": {"has_next_page": False}}),
                        Response(200, {"error": "jiný tvar"})):
            self.logy.clear()
            with self.subTest(odpoved=odpoved.body):
                self.anomalie(lambda server, odpoved=odpoved: server.vynutit.update({("recenze", 1): odpoved}))

    def test_anomalie_stavove_kody(self):
        for status in (400, 403, 500, 502):
            self.logy.clear()
            with self.subTest(status=status):
                self.anomalie(lambda server, status=status: server.vynutit.update({("recenze", 2): Response(status)}))

    def test_anomalie_404_na_prvni_strance(self):
        self.anomalie(lambda server: server.vynutit.update({("recenze", 1): Response(404)}))

    def test_anomalie_datum_a_typy(self):
        def spatne(klic, hodnota):
            def uprav(server):
                server.recenze[1][klic] = hodnota
            return uprav
        for klic, hodnota in (("date", "včera"), ("date", "2099-01-01T00:00:00+00:00"), ("score", "7"),
                              ("is_spoiler", 0), ("tags", "Recommended"), ("mal_id", "5")):
            self.logy.clear()
            with self.subTest(klic=klic, hodnota=hodnota):
                self.anomalie(spatne(klic, hodnota))

    def test_anomalie_limit_stran(self):
        zdroj = novinky.ZDROJE[0]
        puvodni = zdroj.max_stranek
        zdroj.max_stranek = 2
        self.addCleanup(setattr, zdroj, "max_stranek", puvodni)
        state = self.anomalie(lambda server: None)
        self.assertEqual(state.data["texty"]["recenze"]["chyba"]["duvod"], "limit stránek")

    def test_poskozena_fronta_nic_neprepise(self):
        self.slozka.mkdir()
        (self.slozka / "fronta.json").write_text("{nesmysl", encoding="utf-8")
        uspech, state = self.spust(Vypisy([radek_recenze(1, 1101, "2026-11-08")]))
        self.assertFalse(uspech)
        self.assertEqual((self.slozka / "fronta.json").read_text(encoding="utf-8"), "{nesmysl")
        self.assertEqual(self.soubory(), ["fronta.json"])

    def test_tri_429_pusti_stopsource_a_nezapise_nic(self):
        server = Vypisy(self.zakladni_recenze())
        server.vynutit[("recenze", 2)] = Response(429)
        with self.assertRaises(StopSource):
            self.spust(server)
        self.assertEqual(self.soubory(), [])
        self.assertFalse((self.root / "state.json").exists())

    def priprav_stare(self):
        """Soubory kolem hranice 60 dní od 8. 11. 2026 (hranice je 9. 9., smaže se starší)."""
        self.slozka.mkdir(parents=True, exist_ok=True)
        jmena = ["recenze-2026-09-08.jsonl.gz", "posudky-2026-09-08.jsonl.gz", "recenze-2026-09-09.jsonl.gz",
                 "posudky-2026-10-01.jsonl.gz", "prehled-recenze-2026-06-01.jsonl.gz",
                 "prehled-recenze-2026-08-01.jsonl.gz", "prehled-posudky-2026-07-01.jsonl.gz",
                 "smazane-recenze-2026-08-15.json", "smazane-recenze-2026-10-20.json",
                 "poznamka-2026-01-01.txt", "recenze-2026-01-01.jsonl.gz.bak"]
        for jmeno in jmena:
            (self.slozka / jmeno).write_bytes(b"x")
        return jmena

    def test_uklid_maze_jen_stare_podle_data_v_nazvu_a_zapise_seznam_pred_mazanim(self):
        self.priprav_stare()
        seznam = self.root / "smazano.txt"
        uspech, _ = self.spust(Vypisy(self.zakladni_recenze()), smazano=seznam)
        self.assertTrue(uspech)
        zbyva = set(self.soubory())
        # Starší než 60 dní: denní soubory z 8. 9., starší přehled recenzí a smazané z 15. 8.
        # Nejnovější přehled každého výpisu zůstává, i když je starý (základ dalšího celého průchodu).
        self.assertEqual(sorted(seznam.read_text(encoding="utf-8").split()),
                         ["texty/posudky-2026-09-08.jsonl.gz", "texty/prehled-recenze-2026-06-01.jsonl.gz",
                          "texty/recenze-2026-09-08.jsonl.gz", "texty/smazane-recenze-2026-08-15.json"])
        for jmeno in ("recenze-2026-09-09.jsonl.gz", "posudky-2026-10-01.jsonl.gz", "prehled-recenze-2026-08-01.jsonl.gz",
                      "prehled-posudky-2026-07-01.jsonl.gz", "smazane-recenze-2026-10-20.json",
                      "poznamka-2026-01-01.txt", "recenze-2026-01-01.jsonl.gz.bak", "fronta.json"):
            self.assertIn(jmeno, zbyva)
        self.assertNotIn("recenze-2026-09-08.jsonl.gz", zbyva)
        self.assertEqual([l for l in self.logy if "úklid" in l], ["texty úklid: smazáno souborů 4, záznamů fronty 0"])

    def test_uklid_bez_seznamu_a_bez_stareho_nic_nehlasi(self):
        self.spust(Vypisy(self.zakladni_recenze()))
        self.assertFalse(any("úklid" in line for line in self.logy))

    def test_uklid_maze_zastarale_zaznamy_fronty(self):
        self.slozka.mkdir(parents=True)
        fronta = {"v": 1, "datum": "2026-09-01", "cekajici": 3, "anime": {
            "1": {"od": "2026-08-01", "posledni": "2026-09-08", "recenze": 1, "posudky": 0, "zmena": 0},
            "2": {"od": "2026-08-01", "posledni": "2026-09-09", "recenze": 1, "posudky": 0, "zmena": 0},
            "3": {"od": "2026-09-08", "recenze": 1, "posudky": 0, "zmena": 0}}}
        (self.slozka / "fronta.json").write_bytes(novinky.json_bytes(fronta))
        self.spust(Vypisy([radek_recenze(1, 50, "2026-11-08")]))
        nova = json.loads((self.slozka / "fronta.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(nova["anime"], key=int), ["2", "50", "1101", "1102"])
        self.assertNotIn("1", nova["anime"])
        self.assertNotIn("3", nova["anime"])
        self.assertIn("2", nova["anime"])
        self.assertEqual(nova["cekajici"], len(nova["anime"]))
        self.assertIn("texty úklid: smazáno souborů 0, záznamů fronty 2", self.logy)

    def test_uklid_nebezi_po_anomalii(self):
        jmena = self.priprav_stare()
        seznam = self.root / "smazano.txt"
        server = Vypisy(self.zakladni_recenze())
        server.vynutit[("recenze", 1)] = Response(500)
        uspech, _ = self.spust(server, smazano=seznam)
        self.assertFalse(uspech)
        self.assertTrue(set(jmena) <= set(self.soubory()))
        self.assertFalse(seznam.exists())

    def test_uklid_pres_sync_zapise_seznam_do_souboru(self):
        self.root.joinpath("texty").mkdir()
        (self.root / "texty" / "recenze-2026-01-01.jsonl.gz").write_bytes(b"x")
        seznam = self.root / "smazano.txt"
        server = Vypisy([radek_recenze(1, 1101, "2026-11-08")])
        clock = Clock()
        sever = Server()

        def send(method, url, *args):
            if "/reviews/anime" in url or "/recommendations/anime" in url:
                return server(method, url, *args)
            return sever(method, url, *args)

        args = sync.parser().parse_args(["--data", self.temp.name, "--dnes", "2026-11-08", "--zdroj", "tenrai",
                                         "--texty", "--texty-od", "2026-11-07", "--texty-smazano", str(seznam)])
        self.assertEqual(sync.run(args, send, clock.now, clock.sleep, clock.utcnow, log=self.logy.append), 0)
        self.assertEqual(seznam.read_text(encoding="utf-8"), "texty/recenze-2026-01-01.jsonl.gz\n")
        self.assertFalse((self.root / "texty" / "recenze-2026-01-01.jsonl.gz").exists())

    def cely_server(self, recenze):
        return Vypisy(recenze, [radek_posudku("1101-1102", "2026-11-08")], velikost=3)

    def test_cely_pruchod_prehled_zmeny_a_smazane(self):
        recenze = self.zakladni_recenze()
        uspech, state = self.spust(self.cely_server(recenze), cely=True)
        self.assertTrue(uspech)
        self.assertEqual(state.data["texty"]["recenze"]["cely"], "2026-11-08")
        prehled = self.cti("prehled-recenze-2026-11-08.jsonl.gz")
        self.assertEqual(len(prehled), 10)
        self.assertEqual(set(prehled[0]), {"k", "d", "h", "a"})
        self.assertNotIn("x", prehled[0])
        self.assertEqual(self.soubory().count("smazane-recenze-2026-11-08.json"), 0)
        # Po čtvrt roku: jedna recenze upravená, jedna smazaná, jedna nová a stará.
        pozdeji = [radek_recenze(11, 1101, "2026-11-08")] + copy.deepcopy(recenze)
        pozdeji[3]["review"] = "Upravený vymyšlený text."
        upravene_id = pozdeji[3]["mal_id"]
        smazane_id = pozdeji.pop(10)["mal_id"]
        uspech, state = self.spust(self.cely_server(pozdeji), cely=True, dnes=date(2026, 12, 20))
        self.assertTrue(uspech)
        stav = state.data["texty"]["recenze"]
        self.assertEqual((stav["nove"], stav["zmenene"]), (1, 1))
        zmenena = next(r for r in self.cti("recenze-2026-11-08.jsonl.gz") if r["id"] == upravene_id) \
            if upravene_id in (8, 9, 10, 11) else None
        smazane = json.loads((self.slozka / "smazane-recenze-2026-12-20.json").read_text(encoding="utf-8"))
        self.assertEqual(smazane["k"], [str(smazane_id)])
        fronta = json.loads((self.slozka / "fronta.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(sum(a["zmena"] for a in fronta["anime"].values()), 1)

    def test_cely_pruchod_odmitne_zkraceny_vypis(self):
        self.spust(self.cely_server(self.zakladni_recenze()), cely=True)
        pred = self.soubory()
        uspech, state = self.spust(self.cely_server(self.zakladni_recenze()[:3]), cely=True, dnes=date(2026, 12, 20))
        self.assertFalse(uspech)
        self.assertEqual(state.data["texty"]["recenze"]["chyba"]["duvod"], "výpis se zkrátil")
        self.assertEqual([n for n in self.soubory() if "recenze" in n], [n for n in pred if "recenze" in n])


class NapojeniTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.logy = []

    def beh(self, vypisy, *extra):
        server = Server()

        def send(method, url, *args):
            if "/reviews/anime" in url or "/recommendations/anime" in url:
                return vypisy(method, url, *args)
            return server(method, url, *args)

        args = sync.parser().parse_args(["--data", self.temp.name, "--dnes", "2026-11-08", "--zdroj", "tenrai",
                                         "--texty-od", "2026-11-07", *extra])
        result = sync.run(args, send, self.clock.now, self.clock.sleep, self.clock.utcnow, log=self.logy.append)
        return result, server

    def test_bez_prepinace_se_vypisy_nectou(self):
        vypisy = Vypisy([radek_recenze(1, 1101, "2026-11-08")])
        result, server = self.beh(vypisy)
        self.assertEqual((result, vypisy.adresy), (0, []))
        self.assertFalse((self.root / "texty").exists())

    def test_texty_bezi_pred_stahovanim_a_pocitaji_do_limitu(self):
        vypisy = Vypisy([radek_recenze(1, 1101, "2026-11-08")], [radek_posudku("1101-1102", "2026-11-08")])
        result, server = self.beh(vypisy, "--texty")
        self.assertEqual(result, 0)
        self.assertEqual(vypisy.pocet("recenze"), 1)
        self.assertTrue(server.calls)
        state = State(self.temp.name)
        self.assertGreaterEqual(sum(state.data["tenrai_days"].values()), 2 + len(server.calls))
        self.assertEqual(state.data["texty"]["recenze"]["nove"], 1)
        self.assertTrue((self.root / "texty" / "recenze-2026-11-08.jsonl.gz").exists())
        self.assertFalse(any(TAJNE in line for line in self.logy))
        self.assertTrue(any(line.startswith("texty recenze: stran 1, řádků 1, nových 1") for line in self.logy))

    def test_anomalie_vrati_1_ale_stahovani_pokracuje(self):
        vypisy = Vypisy([radek_recenze(1, 1101, "2026-11-08")], [radek_posudku("1101-1102", "2026-11-08")])
        vypisy.vynutit[("recenze", 1)] = Response(200, {"data": [{"mal_id": 5}], "pagination": {"has_next_page": False}})
        result, server = self.beh(vypisy, "--texty")
        self.assertEqual(result, 1)
        self.assertTrue(server.calls)
        self.assertFalse((self.root / "texty" / "recenze-2026-11-08.jsonl.gz").exists())
        self.assertTrue((self.root / "texty" / "posudky-2026-11-08.jsonl.gz").exists())

    def test_texty_cely_funguje_i_bez_texty(self):
        vypisy = Vypisy([radek_recenze(1, 1101, "2026-11-08")], [radek_posudku("1101-1102", "2026-11-08")])
        result, _ = self.beh(vypisy, "--texty-cely")
        self.assertEqual(result, 0)
        self.assertTrue((self.root / "texty" / "prehled-recenze-2026-11-08.jsonl.gz").exists())

    def test_uplnost_texty_v_meta(self):
        vypisy = Vypisy([radek_recenze(1, 1101, "2026-11-08")], [radek_posudku("1101-1102", "2026-11-08")])
        self.beh(vypisy, "--texty")
        meta = build_web.build(self.temp.name, date(2026, 11, 8), log=lambda line: None)
        self.assertEqual(meta["uplnost"]["texty"],
                         {"cteni": "2026-11-08", "nove": 2, "cekajici": 2, "cely": None})

    def test_uplnost_bez_cteni_texty_nema(self):
        meta = build_web.build(self.temp.name, date(2026, 11, 8), log=lambda line: None)
        self.assertNotIn("texty", meta["uplnost"])


if __name__ == "__main__":
    unittest.main()

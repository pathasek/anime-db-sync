"""Čtečky novinek Tenrai: nové recenze a posudky pro vektory a BM25.

Výpisy `/reviews/anime` a `/recommendations/anime` jdou od nejnovějších. Čtečka
čte, dokud celá stránka není starší než poslední viděné datum bez jednoho dne
(posudky mají jen datum), a dává do denních souborů jen to, co tam ještě není.
Při nečekané odpovědi se feed přeruší a nezapíše se nic. Logy nesou jen počty.
"""

from datetime import date, timedelta
import gzip
import hashlib
import io
import json
from pathlib import Path
import re

import tenrai
from state import atomic_write, json_bytes

ZACATEK = date(2026, 11, 3)  # Základ textů je z 5. a 6. 11., s rezervou před nimi.
DATUM = re.compile(r"\d{4}-\d{2}-\d{2}")
DVOJICE = re.compile(r"\d+-\d+")
UCHOVAT_DNI = 60  # Starší denní soubory, přehledy a záznamy fronty maže noční běh.
SOUBORY = (re.compile(r"(?:recenze|posudky)-(\d{4}-\d{2}-\d{2})\.jsonl\.gz"),
           re.compile(r"smazane-(?:recenze|posudky)-(\d{4}-\d{2}-\d{2})\.json"))
PREHLED = re.compile(r"prehled-(recenze|posudky)-(\d{4}-\d{2}-\d{2})\.jsonl\.gz")


class Necekane(Exception):
    """Odpověď má jiný tvar, než čtečka zná. Zpráva je vždy pevný text, nikdy obsah."""


def otisk(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def datum(value):
    """Vrátí řetězec data nebo None. Bez platného ISO data v prvních deseti znacích selže."""
    if value is None:
        return None
    if not isinstance(value, str) or not DATUM.match(value):
        raise ValueError("datum")
    date.fromisoformat(value[:10])
    return value


def kladne(value):
    return type(value) is int and value > 0


def recenze(row):
    entry, reactions = row.get("entry"), row.get("reactions")
    tags, episodes = row.get("tags"), row.get("episodes_watched")
    if not (kladne(row.get("mal_id")) and isinstance(entry, dict) and kladne(entry.get("mal_id"))
            and isinstance(reactions, dict) and type(reactions.get("overall")) is int
            and isinstance(row.get("review"), str) and type(row.get("score")) is int
            and isinstance(tags, list) and all(isinstance(tag, str) for tag in tags)
            and type(row.get("is_spoiler")) is bool and type(row.get("is_preliminary")) is bool
            and (episodes is None or type(episodes) is int)):
        raise ValueError("recenze")
    return {"a": entry["mal_id"], "id": row["mal_id"], "d": datum(row.get("date")), "s": row["score"],
            "t": tags, "sp": row["is_spoiler"], "pr": row["is_preliminary"], "ep": episodes,
            "re": reactions["overall"], "x": row["review"]}


def posudek(row):
    ident, content = row.get("mal_id"), row.get("content")
    if not (isinstance(ident, str) and DVOJICE.fullmatch(ident)
            and (content is None or isinstance(content, str)) and isinstance(row.get("date"), str)):
        raise ValueError("posudek")
    return {"p": ident, "t": content, "d": datum(row["date"])[:10]}


class Zdroj:
    def __init__(self, nazev, zaznam, klic, text, anime, max_stranek, max_cely):
        self.nazev, self.zaznam, self.klic, self.text, self.anime = nazev, zaznam, klic, text, anime
        self.max_stranek, self.max_cely = max_stranek, max_cely


# Posudek nemá vlastní id (dvojice se opakuje, v základu až 650 posudků na jednu),
# proto je klíčem dvojice s otiskem textu.
ZDROJE = (
    Zdroj("recenze", recenze, lambda z: z["id"], lambda z: z["x"], lambda z: (z["a"],), 300, 7000),
    Zdroj("posudky", posudek, lambda z: f"{z['p']}|{otisk(z['t'])}", lambda z: z["t"],
          lambda z: tuple(int(part) for part in z["p"].split("-")), 100, 1300),
)


def zapis_gz(cesta, rows):
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=6, mtime=0) as stream:
        for row in rows:
            stream.write(json_bytes(row) + b"\n")
    atomic_write(cesta, buffer.getvalue())


def cti_gz(cesta):
    try:
        with gzip.open(cesta, "rt", encoding="utf-8") as stream:
            return [json.loads(line) for line in stream]
    except (OSError, EOFError, ValueError):
        raise Necekane("poškozený soubor") from None


class Novinky:
    def __init__(self, klient, state, slozka, dnes, deadline, log=print, cely=False, od=None, smazano=None):
        self.klient, self.slozka, self.dnes, self.deadline = klient, Path(slozka), dnes, deadline
        self.log, self.cely, self.od, self.smazano = log, cely, od or ZACATEK, smazano
        self.den = dnes.isoformat()
        self.horni = (dnes + timedelta(days=1)).isoformat()
        self.stav = state.data.setdefault("texty", {})

    def run(self):
        """Vrací False, když některý feed skončil nečekanou odpovědí. StopSource a StopNight pustí dál."""
        uspech = True
        for zdroj in ZDROJE:
            stav = self.stav.setdefault(zdroj.nazev, {})
            try:
                self.zpracuj(zdroj, stav)
            except Necekane as chyba:
                uspech = False
                stav["chyba"] = {"den": self.den, "duvod": str(chyba)}
                self.log(f"texty {zdroj.nazev}: nečekaná odpověď, nic nezapsáno ({chyba})")
            else:
                stav.pop("chyba", None)
        if uspech:
            self.uklid()
        return uspech

    def k_smazani(self):
        """Názvy starších než UCHOVAT_DNI podle data v názvu. Cizí názvy nikdy, nejnovější přehled výpisu zůstává."""
        nazvy = sorted(path.name for path in self.slozka.iterdir() if path.is_file())
        nejnovejsi = {}
        for jmeno in nazvy:
            if nalez := PREHLED.fullmatch(jmeno):
                nejnovejsi[nalez.group(1)] = jmeno
        vysledek = []
        for jmeno in nazvy:
            prehled = PREHLED.fullmatch(jmeno)
            if prehled and nejnovejsi[prehled.group(1)] != jmeno:
                den = prehled.group(2)
            else:
                nalez = next(filter(None, (vzor.fullmatch(jmeno) for vzor in SOUBORY)), None)
                den = nalez and nalez.group(1)
            if den and (self.dnes - date.fromisoformat(den)).days > UCHOVAT_DNI:
                vysledek.append(jmeno)
        return vysledek

    def uklid(self):
        """Smaže staré denní soubory, přehledy a záznamy fronty.

        Seznam se zapíše dřív, než se maže, aby pojistka workflow nikdy nevolala poplach pro plánované mazání.
        """
        if not self.slozka.is_dir():
            return
        k_smazani = self.k_smazani()
        fronta = self.nacti_frontu()
        hranice = (self.dnes - timedelta(days=UCHOVAT_DNI)).isoformat()
        zastarale = [ident for ident, polozka in fronta["anime"].items()
                     if (polozka.get("posledni") or polozka.get("od") or "") < hranice]
        if self.smazano is not None and k_smazani:
            with open(self.smazano, "a", encoding="utf-8") as stream:
                stream.writelines(f"{self.slozka.name}/{jmeno}\n" for jmeno in k_smazani)
        for jmeno in k_smazani:
            (self.slozka / jmeno).unlink()
        if zastarale:
            for ident in zastarale:
                del fronta["anime"][ident]
            fronta.update(datum=self.den, cekajici=len(fronta["anime"]))
            atomic_write(self.slozka / "fronta.json", json_bytes(fronta))
        if k_smazani or zastarale:
            self.log(f"texty úklid: smazáno souborů {len(k_smazani)}, záznamů fronty {len(zastarale)}")

    def stranka(self, zdroj, cislo):
        odpoved = self.klient.request(zdroj.nazev, tenrai.feed_url(zdroj.nazev, cislo), deadline=self.deadline)
        if odpoved.status == 404 and cislo > 1:
            return None
        if odpoved.status != 200:
            raise Necekane(f"strana {cislo}: HTTP {odpoved.status}")
        telo = odpoved.body
        data, strankovani = telo.get("data"), telo.get("pagination")
        if not isinstance(data, list) or not isinstance(strankovani, dict) \
                or type(strankovani.get("has_next_page")) is not bool:
            raise Necekane(f"strana {cislo}: jiný tvar odpovědi")
        zaznamy = []
        for radek in data:
            try:
                if not isinstance(radek, dict):
                    raise ValueError
                zaznam = zdroj.zaznam(radek)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise Necekane(f"strana {cislo}: neplatný řádek") from None
            if zaznam["d"] and zaznam["d"][:10] > self.horni:
                raise Necekane(f"strana {cislo}: datum v budoucnosti")
            zaznamy.append(zaznam)
        if cislo == 1 and not zaznamy:
            raise Necekane("prázdná první strana")
        return zaznamy, strankovani["has_next_page"]

    def precti(self, zdroj, hranice):
        radky, cislo = {}, 0
        limit = zdroj.max_cely if self.cely else zdroj.max_stranek
        while True:
            cislo += 1
            if cislo > limit:
                raise Necekane("limit stránek")
            vysledek = self.stranka(zdroj, cislo)
            if vysledek is None:
                break
            zaznamy, dalsi = vysledek
            for zaznam in zaznamy:
                radky.setdefault(zdroj.klic(zaznam), zaznam)
            dny = [zaznam["d"][:10] for zaznam in zaznamy if zaznam["d"]]
            if not dalsi or (not self.cely and dny and max(dny) < hranice):
                break
        return radky, cislo

    def predchozi_prehled(self, zdroj):
        soubory = sorted(self.slozka.glob(f"prehled-{zdroj.nazev}-*.jsonl.gz"))
        if not soubory:
            return None
        try:
            return {str(radek["k"]): radek["h"] for radek in cti_gz(soubory[-1])}
        except (KeyError, TypeError):
            raise Necekane("poškozený přehled") from None

    def nacti_den(self, zdroj, cesta):
        if not cesta.exists():
            return {}
        try:
            return {zdroj.klic(z): z for z in cti_gz(cesta)}
        except (KeyError, TypeError):
            raise Necekane("poškozený soubor") from None

    def zpracuj(self, zdroj, stav):
        posledni = stav.get("posledni") or self.od.isoformat()
        hranice = (date.fromisoformat(posledni) - timedelta(days=1)).isoformat()
        radky, stranek = self.precti(zdroj, hranice)
        predchozi = self.predchozi_prehled(zdroj) if self.cely else None
        if predchozi and len(radky) < 0.9 * len(predchozi):
            raise Necekane("výpis se zkrátil")

        def den_of(zaznam):
            return (zaznam["d"] or "")[:10] or self.den

        okno = stav.get("okno", {})
        vyber = {}
        for klic, zaznam in radky.items():
            if den_of(zaznam) >= hranice and str(klic) not in okno:
                vyber.setdefault(den_of(zaznam), {})[klic] = (zaznam, False)
        if predchozi is not None:
            for klic, zaznam in radky.items():
                stary = predchozi.get(str(klic))
                if stary is None or stary != otisk(zdroj.text(zaznam)):
                    vyber.setdefault(den_of(zaznam), {})[klic] = (zaznam, stary is not None)

        nove = zmenene = 0
        fronta = {}
        zapisy = []
        for den, polozky in sorted(vyber.items()):
            cesta = self.slozka / f"{zdroj.nazev}-{den}.jsonl.gz"
            stavajici = self.nacti_den(zdroj, cesta)
            zmeneno = False
            for klic, (zaznam, nahradit) in polozky.items():
                if klic not in stavajici:
                    pole, nove = zdroj.nazev, nove + 1
                elif nahradit and otisk(zdroj.text(stavajici[klic])) != otisk(zdroj.text(zaznam)):
                    pole, zmenene = "zmena", zmenene + 1
                else:
                    continue
                stavajici[klic] = zaznam
                zmeneno = True
                for ident in zdroj.anime(zaznam):
                    fronta.setdefault(ident, {}).setdefault(pole, 0)
                    fronta[ident][pole] += 1
            if zmeneno:
                zapisy.append((cesta, [stavajici[klic] for klic in sorted(stavajici, key=lambda klic: (isinstance(klic, str), klic))]))

        smazane = sorted((k for k in predchozi if k not in {str(k2) for k2 in radky}), key=str) \
            if predchozi else []
        # Napřed fronta, pak soubory: při pádu mezi nimi fronta spíš přebývá, nechybí.
        self.zapis_frontu(fronta)
        for cesta, rows in zapisy:
            zapis_gz(cesta, rows)
        if self.cely:
            zapis_gz(self.slozka / f"prehled-{zdroj.nazev}-{self.den}.jsonl.gz",
                     ({"k": klic, "d": den_of(zaznam), "h": otisk(zdroj.text(zaznam)),
                       "a": list(zdroj.anime(zaznam))}
                      for klic, zaznam in sorted(radky.items(), key=lambda item: (isinstance(item[0], str), item[0]))))
            if smazane:
                atomic_write(self.slozka / f"smazane-{zdroj.nazev}-{self.den}.json",
                             json_bytes({"datum": self.den, "k": smazane}))
            stav["cely"] = self.den

        dated = [zaznam["d"][:10] for zaznam in radky.values() if zaznam["d"]]
        posledni = max([posledni] + dated)
        nova_hranice = (date.fromisoformat(posledni) - timedelta(days=1)).isoformat()
        videno = {**okno, **{str(klic): den_of(zaznam) for klic, zaznam in radky.items()}}
        stav.update(posledni=posledni, cteni=self.den, nove=nove, zmenene=zmenene, stranek=stranek,
                    okno={klic: den for klic, den in sorted(videno.items()) if den >= nova_hranice})
        self.log(f"texty {zdroj.nazev}: stran {stranek}, řádků {len(radky)}, nových {nove}, "
                 f"změněných {zmenene}, smazaných {len(smazane)}")

    def nacti_frontu(self):
        cesta = self.slozka / "fronta.json"
        if not cesta.exists():
            return {"v": 1, "anime": {}}
        try:
            fronta = json.loads(cesta.read_text(encoding="utf-8"))
            if fronta.get("v") != 1 or not isinstance(fronta.get("anime"), dict)                     or not all(isinstance(polozka, dict) for polozka in fronta["anime"].values()):
                raise ValueError
        except (OSError, ValueError, AttributeError):
            raise Necekane("poškozená fronta") from None
        return fronta

    def zapis_frontu(self, pridat):
        fronta = self.nacti_frontu()
        if not pridat:
            return
        for ident, pocty in pridat.items():
            polozka = fronta["anime"].setdefault(str(ident), {"od": self.den, "recenze": 0, "posudky": 0, "zmena": 0})
            polozka["posledni"] = self.den
            for pole, pocet in pocty.items():
                polozka[pole] = polozka.get(pole, 0) + pocet
        fronta.update(datum=self.den, cekajici=len(fronta["anime"]))
        atomic_write(self.slozka / "fronta.json", json_bytes(fronta))


def souhrn(state, slozka):
    """Pole `texty` pro `meta.uplnost`, nebo None, když čtečky ještě neběžely."""
    texty = state.data.get("texty")
    if not isinstance(texty, dict) or not any(isinstance(texty.get(z.nazev), dict) and
                                               texty[z.nazev].get("cteni") for z in ZDROJE):
        return None
    stavy = [texty.get(z.nazev) or {} for z in ZDROJE]
    cesta = Path(slozka) / "fronta.json"
    try:
        cekajici = int(json.loads(cesta.read_text(encoding="utf-8")).get("cekajici", 0)) if cesta.exists() else 0
    except (OSError, ValueError, TypeError, AttributeError):
        cekajici = 0
    vysledek = {"cteni": max(s.get("cteni") or "" for s in stavy) or None,
                "nove": sum(s.get("nove", 0) + s.get("zmenene", 0) for s in stavy), "cekajici": cekajici,
                "cely": min(s.get("cely") or "" for s in stavy) or None}
    chyby = [z.nazev for z, s in zip(ZDROJE, stavy) if s.get("chyba")]
    if chyby:
        vysledek["chyba"] = chyby
    return vysledek

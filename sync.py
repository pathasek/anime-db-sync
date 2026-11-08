"""Noční synchronizace s odděleným stavem a limity obou zdrojů."""

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import signal
import time
import traceback

from anilist import AniList, ANILIST_SVOLENI_DO, metadata
from fronta import Tenrai
from novinky import Novinky
from state import State
from tempo import Client, Limiter, StopNight, StopSource, transport
from uloziste import Storage

MODULES = {"sync.py", "tenrai.py", "anilist.py", "fronta.py", "novinky.py", "state.py",
           "tempo.py", "uloziste.py", "osobni.py", "build_web.py"}


def log_error(error, log=print):
    """Výpis obsahuje pouze typ a místa v našich modulech, bez textu a argumentů."""
    places = []
    for frame in traceback.extract_tb(error.__traceback__):
        path = Path(frame.filename).resolve()
        if path.parent == Path(__file__).resolve().parent and path.name in MODULES:
            places.append(f"{path.name}:{frame.lineno}:{frame.name}")
    log(f"stav: chyba {type(error).__name__}; místa " + ", ".join(places))


def parser():
    cli = argparse.ArgumentParser(description="Noční stažení databáze anime")
    cli.add_argument("--data", required=True, help="Složka soukromých dat")
    cli.add_argument("--minutes", type=float, default=270)
    cli.add_argument("--rate", type=float, default=108)
    cli.add_argument("--anilist-rate", type=float, default=20)
    cli.add_argument("--zdroj", choices=("tenrai", "anilist", "oba"), default="oba")
    cli.add_argument("--vzorek", type=int)
    cli.add_argument("--osobni")
    cli.add_argument("--dnes", type=date.fromisoformat)
    cli.add_argument("--max-dotazu", type=int)
    cli.add_argument("--texty", action="store_true",
                     help="Před stahováním Tenrai přečíst novinky recenzí a posudků")
    cli.add_argument("--texty-cely", action="store_true",
                     help="Čtvrtletní celý průchod výpisů (úpravy a smazané), platí i bez --texty")
    cli.add_argument("--texty-smazano", help="Soubor, kam se před úklidem zapíší plánovaně mazané cesty texty/…")
    cli.add_argument("--texty-od", type=date.fromisoformat,
                     help="Počáteční datum, dokud čtečky nemají vlastní poslední viděné")
    return cli


def run(args, send=transport, now=time.monotonic, sleep=time.sleep, utcnow=None, log=print):
    """Veškeré závislosti na síti a čase jsou nahraditelné v testech."""
    utcnow = utcnow or (lambda: datetime.now(timezone.utc))
    today = args.dnes or utcnow().date()
    started = now()
    deadline = started + max(0, args.minutes) * 60
    al_deadline = deadline
    if args.dnes is None and today < ANILIST_SVOLENI_DO:
        expiry = datetime.combine(ANILIST_SVOLENI_DO, datetime.min.time(), timezone.utc).timestamp()
        al_deadline = min(deadline, started + max(0, expiry - utcnow().timestamp()))
    sources = ["anilist", "tenrai"] if args.zdroj == "oba" else [args.zdroj]
    if args.vzorek is not None and args.zdroj == "oba":
        sources = ["tenrai", "anilist"]
    states = {source: State(args.data, source) for source in sources}
    stores = {source: Storage(args.data, source) for source in sources}
    counts = reported_count = 0
    saved_time = started
    clients = []
    al_job = None
    result = 0

    def summary():
        minutes = max((now() - started) / 60, 1 / 60)
        for client in clients:
            for endpoint, counter in sorted(client.counts.items()):
                log(f"{client.source} {endpoint}: staženo {counter['stazeno']}, chyb {counter['chyb']}, "
                    f"pryč {counter['pryc']}, dotazů {counter['dotazu']}, "
                    f"tempo {counter['dotazu'] / minutes:.2f}/min")
        al = states["anilist"].data["anilist"] if "anilist" in states else {}
        log(f"stav: řezů {len(al.get('snapshot', {}).get('todo', []))}, "
            f"dostahování {len(al.get('pending', {}))}, čas {now() - started:.1f} s")

    def save(report=False):
        nonlocal reported_count, saved_time, result
        # Stav postupu musí být bezpečně uložen před odvozenými daty.
        for source in sources:
            stores[source].flush()
            states[source].save()
        saved_time = now()
        if al_job is not None:
            try:
                al_job.nearby()
                states["anilist"].save()
                stores["anilist"].write_json("anilist", "meta", metadata(al_job.al, stores["anilist"]))
            except Exception as error:
                log_error(error, log)
                result = 1
        if report:
            reported_count = counts
            summary()

    def before():
        if now() >= deadline or (args.max_dotazu is not None and counts >= args.max_dotazu):
            raise StopNight()
        report = counts - reported_count >= 500
        if report or now() - saved_time >= 120:
            save(report=report)

    def after():
        nonlocal counts
        counts += 1

    def terminate(signum, frame):
        raise StopNight()

    previous = None
    try:
        try:
            previous = signal.signal(signal.SIGTERM, terminate)
        except ValueError:
            pass
        for source in sources:
            state, storage = states[source], stores[source]
            rate = min(args.rate, 108) if source == "tenrai" else min(args.anilist_rate, 20)
            limiter = Limiter(rate, now, sleep, state=state if source == "tenrai" else None, utcnow=utcnow)
            client = Client(source, limiter, send, before, after, log)
            clients.append(client)
            if source == "tenrai":
                job = Tenrai(client, state, storage, today, deadline, sample=args.vzorek)
            else:
                sample_ids = None
                if args.vzorek is not None:
                    mal = states.get("tenrai") or State(args.data, "tenrai")
                    pages = mal.data.get("list_page_ids", {})
                    sample_ids = {ident for page in range(1, (args.vzorek + 24) // 25 + 1)
                                  for ident in pages.get(str(page), [])}
                al_job = job = AniList(client, state, storage, today, al_deadline,
                    args.osobni, wall_now=lambda: utcnow().timestamp(), log=log,
                    today_fn=lambda: args.dnes or utcnow().date(), sample_ids=sample_ids)
            try:
                if source == "tenrai" and (args.texty or args.texty_cely):
                    texty = Novinky(client, state, Path(args.data) / "texty", today, deadline, log,
                                    cely=args.texty_cely, od=args.texty_od, smazano=args.texty_smazano)
                    if not texty.run():
                        result = 1
                job.run()
            except StopSource:
                log(f"{source}: zdroj pro tuto noc ukončen")
            except StopNight:
                raise
            except Exception as error:
                log_error(error, log)
                result = 1
            # Druhý zdroj i další proces uvidí hotové bloky a postup.
            save()
    except (StopNight, KeyboardInterrupt):
        log("stav: stahování ukončeno")
    except Exception as error:
        log_error(error, log)
        result = 1
    finally:
        try:
            save(report=True)
        except Exception as error:
            log_error(error, log)
            result = 1
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if (args.rate <= 0 or args.anilist_rate <= 0 or args.minutes < 0
            or (args.max_dotazu is not None and args.max_dotazu < 0)
            or (args.vzorek is not None and args.vzorek <= 0)):
        parser().error("Tempo a vzorek musí být kladné, čas a počet dotazů nezáporné")
    try:
        return run(args)
    except Exception as error:
        log_error(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

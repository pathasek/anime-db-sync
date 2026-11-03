"""Deterministický webový export ze soukromých surových dat."""

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import re
import shutil
import uuid

from anilist import ANILIST_SVOLENI_DO, metadata
from osobni import neighborhood
from state import State, atomic_write, json_bytes, replace
from uloziste import Storage, MAX_BYTES

COLS = ["id", "title", "title_english", "title_japanese", "synonyma", "type", "episodes",
        "delka_min", "status", "aired_od", "aired_do", "season", "year", "source", "rating",
        "score", "scored_by", "members", "favorites", "rank", "popularity", "genres",
        "explicit_genres", "themes", "demographics", "studios", "producers", "img"]
DICTS = ("genres", "explicit_genres", "themes", "demographics", "studios", "producers")


def duration_minutes(text):
    if not text:
        return None
    total = 0.0
    found = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(hr|min|sec)\b", text):
        total += float(value) * {"hr": 60, "min": 1, "sec": 1 / 60}[unit]
        found = True
    if not found:
        return None
    return int(total) if total.is_integer() else round(total, 6)


def day_only(value):
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def safe_remove(path, parent):
    """Mažeme jen ověřený přímý podadresář určené datové složky."""
    if path.is_symlink() or path.resolve().parent != parent.resolve():
        raise ValueError("Neplatná cesta webového adresáře")
    if path.exists():
        shutil.rmtree(path)


def build(directory, today=None, al_tags="auto", log=print):
    today = today or datetime.now(timezone.utc).date()
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    web = root / "web"
    if web.is_symlink() or web.resolve().parent != root:
        raise ValueError("Neplatná cesta webového adresáře")
    state, al_state, storage = State(root), State(root, "anilist"), Storage(root)
    anime = {ident: row for ident, row in storage.items("tenrai", "list") if not state.gone(ident)}
    active = set(anime)
    mal_data = {endpoint: {ident: row for ident, row in storage.items("tenrai", endpoint) if ident in active}
                for endpoint in ("recs", "full", "staff", "chars", "stats")}
    media = dict(storage.items("anilist", "media"))
    al_mode = ("cele" if today < ANILIST_SVOLENI_DO else "okoli") if al_tags == "auto" else al_tags
    nearby = set(al_state.data["anilist"].get("neighborhood", [])) | neighborhood(storage, state=state)
    dictionaries = {key: {} for key in DICTS}
    tag_dictionary = storage.read_json("anilist", "tagy", {})
    if isinstance(tag_dictionary, list):
        tag_dictionary = {str(tag["id"]): tag for tag in tag_dictionary}
    dictionaries["tags"] = {str(ident): [tag.get("name"), tag.get("category"),
                                         bool(tag.get("isAdult")), tag.get("description")]
                              for ident, tag in tag_dictionary.items()}
    core = []
    syn = {}
    for ident, row in sorted(anime.items()):
        references = {}
        for key in DICTS:
            references[key] = [item["mal_id"] for item in row.get(key, [])]
            for item in row.get(key, []):
                dictionaries[key][str(item["mal_id"])] = item.get("name")
        values = {**row, **references, "id": ident, "synonyma": row.get("title_synonyms", []),
                  "delka_min": duration_minutes(row.get("duration")),
                  "aired_od": day_only((row.get("aired") or {}).get("from")),
                  "aired_do": day_only((row.get("aired") or {}).get("to"))}
        core.append([values.get(key) for key in COLS])
        syn.setdefault(ident // 1000, {})[str(ident)] = row.get("synopsis")

    mal_recs = sorted([ident, target, votes] for ident, recs in mal_data["recs"].items()
                      for target, votes in recs if target in active)
    al_recs = set()
    tags = {}
    al_blocks = {}
    al_present = set()
    for ident, row in sorted(media.items()):
        mal = row.get("idMal")
        if mal not in active:
            continue
        al_present.add(mal)
        if al_mode == "cele" or mal in nearby:
            tags[str(mal)] = row.get("tags", [])
            al_blocks.setdefault(mal // 1000, {})[str(mal)] = {**row, "al_id": ident}
        for target, target_mal, rating in row.get("recommendations", []):
            if target_mal in active:
                al_recs.add((mal, target_mal, rating))

    staff = {"lide": {}, "anime": {}}
    for ident, rows in sorted(mal_data["staff"].items()):
        entries = []
        for row in rows:
            person = row["person"]
            pid = person["mal_id"]
            staff["lide"][str(pid)] = [person.get("name"), person.get("img")]
            entries.append([pid, row.get("positions", [])])
        staff["anime"][str(ident)] = entries
    chars = {}
    for ident, rows in sorted(mal_data["chars"].items()):
        block = chars.setdefault(ident // 1000, {"postavy": {}, "lide": {}, "anime": {}})
        entries = []
        for row in rows:
            character = row["character"]
            cid = character["mal_id"]
            block["postavy"][str(cid)] = [character.get("name"), character.get("img")]
            voices = []
            for actor in row.get("voice_actors", []):
                person = actor["person"]
                pid = person["mal_id"]
                block["lide"][str(pid)] = [person.get("name"), person.get("img")]
                voices.append([pid, actor.get("language")])
            entries.append([cid, row.get("role"), row.get("favorites", 0), voices])
        block["anime"][str(ident)] = entries
    stats = {}
    for ident, row in sorted(mal_data["stats"].items()):
        votes = {score["score"]: score["votes"] for score in row.get("scores", [])}
        stats[str(ident)] = [row.get(key, 0) for key in
                             ("watching", "completed", "on_hold", "dropped", "plan_to_watch", "total")]
        stats[str(ident)].append([votes.get(score, 0) for score in range(1, 11)])
    relations = {}
    for ident, row in sorted(mal_data["full"].items()):
        relations[str(ident)] = [[relation["relation"], targets] for relation in row.get("relations") or []
                                 if (targets := [entry["mal_id"] for entry in relation.get("entry", [])
                                                 if entry.get("type", "").lower() == "anime"
                                                 and entry["mal_id"] in active])]
    files = {"core.json": {"sloupce": COLS, "radky": core}, "dict.json": dictionaries,
             "recs.json": {"mal": mal_recs, "al": sorted(al_recs)},
             "staff.json": staff, "stats.json": stats, "rel.json": relations, "al_tags.json": tags}
    files.update({f"syn/{block:04d}.json": rows for block, rows in syn.items()})
    files.update({f"chars/{block:04d}.json": rows for block, rows in chars.items()})
    files.update({f"al/{block:04d}.json": rows for block, rows in al_blocks.items()})
    count = len(active)
    complete = {"list": 1.0 if count else 0.0,
                **{endpoint: len(rows) / count if count else 0.0 for endpoint, rows in mal_data.items()},
                "anilist": len(al_present) / count if count else 0.0}
    errors = skipped = 0
    for endpoint, records in {**state.data["items"], **al_state.data["items"]}.items():
        for ident, record in records.items():
            if endpoint in mal_data or endpoint == "list":
                if int(ident) not in active:
                    continue
            errors += int(record[1] > 0)
            skipped += int(record[2] > today.toordinal())
    gone = {int(ident) for endpoint, records in state.data["items"].items()
            if endpoint in ("list", "recs", "full", "staff", "chars", "stats")
            for ident, record in records.items() if record[3]}
    al_meta = metadata(al_state.data["anilist"], storage)
    meta = {"verze": 1, "datum": today.isoformat(), "pocet": count, "uplnost": complete,
            "chyb": errors, "preskoceno": skipped, "pryc": len(gone), "al_tagy": al_mode,
            "anilist": {key: al_meta[key] for key in ("rezim", "datum", "pocet", "celkem", "rozdil")},
            "velikosti": {name: len(json_bytes(value)) for name, value in files.items()}}
    # Velikost meta.json obsahuje i samotné meta.json; počet číslic rychle ustálí.
    meta["velikosti"]["meta.json"] = 0
    while meta["velikosti"]["meta.json"] != len(json_bytes(meta)):
        meta["velikosti"]["meta.json"] = len(json_bytes(meta))
    files["meta.json"] = meta
    # Dědění ACL soukromého checkoutu funguje i pod omezeným tokenem Windows.
    staging = root / (".web-novy-" + uuid.uuid4().hex)
    staging.mkdir()
    backup = root / ".web-predchozi"
    try:
        for name, value in sorted(files.items()):
            content = json_bytes(value)
            if len(content) > MAX_BYTES:
                raise ValueError("Webový soubor přesahuje povolených 50 MB")
            atomic_write(staging / name, content)
        safe_remove(backup, root)
        if web.exists():
            replace(web, backup)
        try:
            replace(staging, web)
        except BaseException:
            if backup.exists() and not web.exists():
                replace(backup, web)
            raise
        safe_remove(backup, root)
    finally:
        safe_remove(staging, root)
    log(f"stav: staženo {count}, pryč {len(gone)}, velikost {sum(meta['velikosti'].values())} bajtů")
    return meta


def main(argv=None):
    cli = argparse.ArgumentParser(description="Sestavení webových dat")
    cli.add_argument("--data", required=True)
    cli.add_argument("--dnes", type=date.fromisoformat)
    cli.add_argument("--al-tagy", choices=("auto", "cele", "okoli"), default="auto")
    args = cli.parse_args(argv)
    try:
        build(args.data, args.dnes, args.al_tagy)
        return 0
    except Exception:
        print("stav: chyba sestavení webových dat")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

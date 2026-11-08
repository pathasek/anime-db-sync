"""Deterministický webový export ze soukromých surových dat."""

import argparse
from datetime import date, datetime, timezone
import gzip
import html
from itertools import groupby
from pathlib import Path
import re
import shutil
import uuid

import novinky
from anilist import metadata
from osobni import neighborhood
from state import State, atomic_write, json_bytes, replace
from uloziste import Storage, MAX_BYTES

COLS = ["id", "title", "title_english", "type", "episodes",
        "delka_min", "status", "aired_od", "aired_do", "season", "year", "source", "rating",
        "score", "scored_by", "members", "favorites", "rank", "popularity", "genres",
        "explicit_genres", "themes", "demographics", "studios", "producers", "img"]
DICTS = ("genres", "explicit_genres", "themes", "demographics", "studios", "producers")
AL_COLS = ["id", "al", "format", "zeme", "adult", "prumer", "popularita", "oblibeni", "tagy"]
BLOCKS = {"syn": 500, "chars": 1000, "al_detail": 100, "al_lide": 100}
AL_EXCLUDE = {"id", "tags", "recommendations", "staff", "characters", "idMal", "format",
              "countryOfOrigin", "isAdult", "averageScore", "popularity", "favourites"}


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


def stored_rows(storage, source, endpoint):
    """Průchod drží jen právě čtený blok surových dat."""
    try:
        for ident, row in storage.items(source, endpoint):
            storage.cache.clear()
            yield ident, row
    finally:
        storage.cache.clear()


def voice_language(actor):
    return actor.get("language") or "Unknown"


def base_role(text):
    return re.sub(r"\s*\([^)]*\)", "", text).strip() or text


def description_text(text):
    text = re.sub(r"<br\s*/?\s*>", "\n", text or "", flags=re.IGNORECASE)
    return html.unescape(re.sub(r"<[^>]*>", "", text)).strip()


def build(directory, today=None, al_tags="cele", log=print):
    if al_tags not in ("cele", "okoli"):
        raise ValueError("Neplatný režim tagů AniListu")
    today = today or datetime.now(timezone.utc).date()
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    web = root / "web"
    if web.is_symlink() or web.resolve().parent != root:
        raise ValueError("Neplatná cesta webového adresáře")
    state, al_state, storage = State(root), State(root, "anilist"), Storage(root)
    anime = {ident: row for ident, row in stored_rows(storage, "tenrai", "list")
             if not state.gone(ident)}
    active = set(anime)
    count = len(active)
    nearby = set(al_state.data["anilist"].get("neighborhood", [])) | neighborhood(storage, state=state)
    storage.cache.clear()
    al_meta = metadata(al_state.data["anilist"], storage)
    storage.cache.clear()
    media = {}
    al_present = set()
    for ident, row in stored_rows(storage, "anilist", "media"):
        mal = row.get("idMal")
        if mal in active:
            al_present.add(mal)
            if al_tags == "cele" or mal in nearby:
                if mal not in media or row["id"] > media[mal]["id"]:
                    media[mal] = row
    complete = {"list": 1.0 if count else 0.0, "anilist": len(al_present) / count if count else 0.0}
    pages = state.data.get("list_pages", 0)
    if pages:
        complete["list"] = min(1.0, sum(bool(state.data["pages"].get(f"asc:{page}", [0])[0])
                                        for page in range(1, pages + 1)) / pages)

    def mal_rows(endpoint):
        loaded = 0
        for ident, row in stored_rows(storage, "tenrai", endpoint):
            if ident in active:
                loaded += 1
                yield ident, row
        complete[endpoint] = loaded / count if count else 0.0

    texty = novinky.souhrn(state, root / "texty")
    if texty is not None:
        complete["texty"] = texty

    sizes = {}
    staging = root / (".web-novy-" + uuid.uuid4().hex)
    staging.mkdir()
    backup = root / ".web-predchozi"

    def write(name, value):
        content = json_bytes(value)
        if len(content) > MAX_BYTES:
            raise ValueError("Webový soubor přesahuje povolených 50 MB")
        atomic_write(staging / name, content)
        if name != "meta.json":
            sizes[name] = [len(content), len(gzip.compress(content, compresslevel=9, mtime=0))]
        return len(content)

    try:
        dictionaries = {key: {} for key in DICTS}
        tag_dictionary = storage.read_json("anilist", "tagy", {})
        if isinstance(tag_dictionary, list):
            tag_dictionary = {str(tag["id"]): tag for tag in tag_dictionary}
        dictionaries["tags"] = {str(ident): [tag.get("name"), tag.get("category"),
                                             bool(tag.get("isAdult")), tag.get("description")]
                                  for ident, tag in tag_dictionary.items()}
        del tag_dictionary
        core = {key: [] for key in COLS}
        for ident in sorted(anime):
            row = anime[ident]
            references = {}
            for key in DICTS:
                references[key] = [item["mal_id"] for item in row.get(key, [])]
                for item in row.get(key, []):
                    dictionaries[key][str(item["mal_id"])] = item.get("name")
            values = {**row, **references, "id": ident,
                      "delka_min": duration_minutes(row.get("duration")),
                      "aired_od": day_only((row.get("aired") or {}).get("from")),
                      "aired_do": day_only((row.get("aired") or {}).get("to"))}
            for key in COLS:
                core[key].append(values.get(key))
        write("core.json", {"pocet": count, "data": core})
        del core

        core = {key: [] for key in AL_COLS}
        for mal in sorted(media):
            row = media[mal]
            tags = [value for tag in sorted(row.get("tags", []), key=lambda tag: (-tag[1], tag[0]))
                    for value in (tag[0], tag[1], int(bool(tag[2])))]
            values = [mal, row["id"], row.get("format"), row.get("countryOfOrigin"),
                      int(bool(row.get("isAdult"))), row.get("averageScore"), row.get("popularity"),
                      row.get("favourites"), tags]
            for key, value in zip(AL_COLS, values):
                core[key].append(value)
        write("al_core.json", {"pocet": len(media), "data": core})
        del core

        titles = {}
        for ident in sorted(anime):
            row, al = anime[ident], media.get(ident, {})
            japanese = row.get("title_japanese") or None
            seen = {text.casefold() for text in (row.get("title"), row.get("title_english"), japanese)
                    if text}
            title = al.get("title") or {}
            aliases = []
            for text in [*(row.get("title_synonyms") or []), title.get("romaji"), title.get("english"),
                         title.get("native"), *(al.get("synonyms") or [])]:
                if text and text.strip() and text.casefold() not in seen:
                    seen.add(text.casefold())
                    aliases.append(text)
            if japanese or aliases:
                titles[str(ident)] = [japanese, aliases]
        write("titles.json", titles)
        del titles
        for block, ids in groupby(sorted(anime), key=lambda ident: ident // BLOCKS["syn"]):
            syn = {}
            for ident in ids:
                text = anime[ident].get("synopsis")
                if not text or not text.strip():
                    text = description_text(media.get(ident, {}).get("description"))
                if text:
                    syn[str(ident)] = text
            if syn:
                write(f"syn/{block:04d}.json", syn)
            del syn

        mal_recs = sorted((ident, target, votes) for ident, rows in mal_rows("recs")
                          for target, votes in rows if target in active)
        al_recs = sorted({(mal, target_mal, rating) for mal, row in media.items()
                          for target, target_mal, rating in row.get("recommendations", [])
                          if target_mal in active})
        write("recs.json", {"mal": [value for edge in mal_recs for value in edge],
                            "al": [value for edge in al_recs for value in edge]})
        del mal_recs, al_recs

        dictionaries["role"] = sorted({base_role(position) for ident, rows in mal_rows("staff")
                                       for row in rows for position in row.get("positions", [])})
        roles = {name: index for index, name in enumerate(dictionaries["role"])}
        staff = {"lide": {}, "anime": {}}
        for ident, rows in mal_rows("staff"):
            people = {}
            for row in rows:
                person = row["person"]
                pid = person["mal_id"]
                staff["lide"][str(pid)] = person.get("name")
                entry = people.setdefault(pid, [pid])
                for position in row.get("positions", []):
                    index = roles[base_role(position)]
                    if index not in entry[1:]:
                        entry.append(index)
            staff["anime"][str(ident)] = list(people.values())
        write("staff.json", staff)
        del staff, roles

        dictionaries["jazyky"] = sorted({voice_language(actor) for ident, rows in mal_rows("chars")
                                         for row in rows for actor in row.get("voice_actors", [])})
        languages = {name: index for index, name in enumerate(dictionaries["jazyky"])}
        write("dict.json", dictionaries)
        del dictionaries
        cast = {"postavy": {}, "lide": {}, "anime": {}}
        for block, rows in groupby(mal_rows("chars"), key=lambda item: item[0] // BLOCKS["chars"]):
            chars = {"postavy": {}, "lide": {}, "anime": {}}
            for ident, characters in rows:
                entries, cast_entries = [], []
                for row in characters:
                    character = row["character"]
                    cid = character["mal_id"]
                    chars["postavy"][str(cid)] = cast["postavy"][str(cid)] = character.get("name")
                    main = int(row.get("role") == "Main")
                    entry = [cid, main, row.get("favorites", 0)]
                    voices = {"Japanese": [], "English": []}
                    for actor in row.get("voice_actors", []):
                        person = actor["person"]
                        pid, language = person["mal_id"], voice_language(actor)
                        chars["lide"][str(pid)] = person.get("name")
                        entry.extend([pid, languages[language]])
                        if language in voices and pid not in voices[language]:
                            voices[language].append(pid)
                            cast["lide"][str(pid)] = person.get("name")
                    entries.append(entry)
                    cast_entries.append([cid, main, voices["Japanese"], voices["English"]])
                chars["anime"][str(ident)] = entries
                cast["anime"][str(ident)] = cast_entries
            write(f"chars/{block:04d}.json", chars)
            del chars
        write("cast.json", cast)
        del cast, languages

        stats = {}
        for ident, row in mal_rows("stats"):
            votes = {score["score"]: score["votes"] for score in row.get("scores", [])}
            stats[str(ident)] = [row.get(key, 0) for key in
                                 ("watching", "completed", "on_hold", "dropped", "plan_to_watch", "total")]
            stats[str(ident)].append([votes.get(score, 0) for score in range(1, 11)])
        write("stats.json", stats)
        del stats
        relations = {}
        for ident, row in mal_rows("full"):
            relations[str(ident)] = [[relation["relation"], targets] for relation in row.get("relations") or []
                                     if (targets := [entry["mal_id"] for entry in relation.get("entry", [])
                                                     if entry.get("type", "").lower() == "anime"
                                                     and entry["mal_id"] in active])]
        write("rel.json", relations)
        del relations
        for name in ("al_detail", "al_lide"):
            for block, ids in groupby(sorted(media), key=lambda ident: ident // BLOCKS[name]):
                rows = {}
                for ident in ids:
                    row = media[ident]
                    if name == "al_detail":
                        rows[str(ident)] = {key: value for key, value in row.items() if key not in AL_EXCLUDE}
                        rows[str(ident)]["al"] = row["id"]
                    else:
                        rows[str(ident)] = {key: row.get(key, []) for key in ("staff", "characters")}
                write(f"{name}/{block:04d}.json", rows)
                del rows

        errors = skipped = 0
        for endpoint, records in {**state.data["items"], **al_state.data["items"]}.items():
            for ident, record in records.items():
                if endpoint in ("list", "recs", "full", "staff", "chars", "stats") and int(ident) not in active:
                    continue
                errors += int(record[1] > 0)
                skipped += int(record[2] > today.toordinal())
        gone = {int(ident) for endpoint, records in state.data["items"].items()
                if endpoint in ("list", "recs", "full", "staff", "chars", "stats")
                for ident, record in records.items() if record[3]}
        meta = {"verze": 2, "datum": today.isoformat(), "pocet": count, "bloky": BLOCKS.copy(),
                "uplnost": complete, "chyb": errors, "preskoceno": skipped, "pryc": len(gone),
                "al_tagy": al_tags, "anilist": al_meta, "velikosti": sizes}
        meta_size = write("meta.json", meta)
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
    total_size = sum(size[0] for size in sizes.values()) + meta_size
    log(f"stav: staženo {count}, pryč {len(gone)}, velikost {total_size} bajtů")
    return meta


def main(argv=None):
    cli = argparse.ArgumentParser(description="Sestavení webových dat")
    cli.add_argument("--data", required=True)
    cli.add_argument("--dnes", type=date.fromisoformat)
    cli.add_argument("--al-tagy", choices=("cele", "okoli"), default="cele")
    args = cli.parse_args(argv)
    try:
        build(args.data, args.dnes, args.al_tagy)
        return 0
    except Exception:
        print("stav: chyba sestavení webových dat")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

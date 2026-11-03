"""Malá množina osobních anime, sousedů a aktuálně vysílaných titulů."""

import json
from pathlib import Path
import re


def normalize(title):
    return "".join(char for char in (title or "").casefold() if char.isalnum())


def read_list(directory, name):
    if directory is None:
        return []
    path = Path(directory) / name
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, list) else []


def neighborhood(storage, directory=None, state=None):
    anime = {ident: row for ident, row in storage.items("tenrai", "list")
             if state is None or not state.gone(ident)}
    watched = set()
    for row in read_list(directory, "anime_list.json"):
        match = re.match(r"https://myanimelist\.net/anime/(\d+)(?:/|$)", row.get("mal_url", ""))
        if match:
            watched.add(int(match[1]))
    titles = {}
    for ident, row in anime.items():
        for title in [row.get("title"), row.get("title_english"), row.get("title_japanese"),
                      *row.get("title_synonyms", [])]:
            key = normalize(title)
            if key:
                titles.setdefault(key, set()).add(ident)
    for row in read_list(directory, "plan_to_watch.json"):
        matches = titles.get(normalize(row.get("name")), set())
        if len(matches) == 1:
            watched.update(matches)
    result = set(watched)
    for ident in sorted(watched):
        recs = storage.get("tenrai", "recs", ident, [])
        result.update(target for target, votes in sorted(recs, key=lambda x: (-x[1], x[0]))[:10])
    result.update(ident for ident, row in anime.items()
                  if row.get("status") in ("Currently Airing", "Not yet aired"))
    return {ident for ident in result if state is None or not state.gone(ident)}

"""Adresy Tenrai a zhuštění bez ukládání odvoditelných variant."""

from urllib.parse import urlencode

BASE = "https://api.tenrai.org/v1"
ENDPOINTS = ("list", "recs", "full", "staff", "chars", "stats")
PATHS = {"recs": "recommendations", "full": "full", "staff": "staff",
         "chars": "characters", "stats": "statistics"}
# Souhrnné výpisy od nejnovějších: recenze po 50, posudky po 100 na stránku.
FEEDS = {"recenze": ("reviews/anime", (("spoilers", "true"), ("preliminary", "true"))),
         "posudky": ("recommendations/anime", ())}


def url(endpoint="list", ident=None, page=1, mode="asc"):
    if endpoint != "list":
        return f"{BASE}/anime/{int(ident)}/{PATHS[endpoint]}"
    params = {"page": page}
    if mode in ("airing", "upcoming"):
        params["status"] = mode
    else:
        params.update(order_by="mal_id", sort=mode)
    return BASE + "/anime?" + urlencode(params)


def feed_url(feed, page):
    path, params = FEEDS[feed]
    return f"{BASE}/{path}?" + urlencode((("page", int(page)),) + params)


def compact(value, path=()):
    if isinstance(value, list):
        return [compact(item, path + ("item",)) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "url" and path not in (("external", "item"), ("streaming", "item")):
            continue
        if key == "images":
            image = (item or {}).get("jpg", {}).get("image_url")
            result["img"] = image.removeprefix("https://cdn.myanimelist.net") if image else None
        elif key == "trailer":
            result[key] = {"youtube_id": (item or {}).get("youtube_id")}
        else:
            result[key] = compact(item, path + (key,))
    return result


def reduce(endpoint, data):
    expected = dict if endpoint in ("stats", "full", "list") else list
    if not isinstance(data, expected):
        raise TypeError("Neplatný tvar položky")
    if endpoint == "stats":
        return data
    if endpoint == "recs":
        if not isinstance(data, list):
            raise TypeError("Neplatná doporučení")
        return [[entry["mal_id"], row.get("votes", 0)] for row in data
                if isinstance(row, dict) and isinstance(entry := row.get("entry"), dict)
                and type(entry.get("mal_id")) is int and entry["mal_id"] > 0]
    if endpoint == "full":
        return compact({key: data.get(key) for key in ("relations", "theme", "external", "streaming")})
    return compact(data)

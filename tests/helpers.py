"""Falešný server kontroluje hloubku, tvar dotazů a síťové hlavičky."""

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from tempo import Response, USER_AGENT


import os
import tempfile
from unittest.mock import patch


class TemporaryDirectory(tempfile.TemporaryDirectory):
    """Používá tempfile; adresář zdědí ACL přístupná i omezenému runneru Windows."""

    def __init__(self, **kwargs):
        mkdir = os.mkdir

        def inherited(path, mode=0o777):
            return mkdir(path, 0o777)

        # Python 3.12 na Windows nastavuje pro režim 0700 výlučná ACL.
        # Změna platí jen během vytvoření testovacího adresáře.
        with patch("tempfile._os.mkdir", inherited):
            super().__init__(dir=Path(__file__).parent, **kwargs)


def fixture(name):
    return json.loads((Path(__file__).parent / "fixtures" / (name + ".json")).read_text(encoding="utf-8"))


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []
        self.epoch = datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp()

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds

    def utcnow(self):
        return datetime.fromtimestamp(self.epoch + self.value, timezone.utc)


def media(ident, popularity=20, adult=False):
    return {"id": ident, "idMal": ident + 100000, "format": "TV", "status": "FINISHED",
            "isAdult": adult, "popularity": popularity, "title": {"romaji": f"Testovací anime {ident}"},
            "updatedAt": 100, "tags": [], "recommendations": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            "relations": {"edges": []}}


class Server:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []
        self.fail_at = None
        self.alias_fail = False
        self.missing = set()
        self.rec_pages = {}
        self.tags_calls = 0
        self.updated = None
        self.known_pages = fixture("tenrai")["data"]

    def __call__(self, method, url, headers, payload, timeout):
        assert headers["User-Agent"] == USER_AGENT
        assert timeout == 30
        self.calls.append((url, copy.deepcopy(payload)))
        if self.fail_at == len(self.calls):
            raise RuntimeError("TAJNY_TITUL")
        if method == "GET":
            assert headers["Accept"] == "application/json"
            parts = urlsplit(url)
            if parts.path == "/v1/anime":
                params = parse_qs(parts.query)
                if "status" in params:
                    status = {"airing": "Currently Airing", "upcoming": "Not yet aired"}[params["status"][0]]
                    data = [row for row in self.known_pages if row.get("status") == status]
                else:
                    data = sorted(self.known_pages, key=lambda row: row["mal_id"],
                                  reverse=params.get("sort") == ["desc"])
                return Response(200, {"data": copy.deepcopy(data), "pagination": {"has_next_page": False}})
            endpoint = {"recommendations": "recs", "characters": "chars", "statistics": "stats"}.get(
                parts.path.split("/")[-1], parts.path.split("/")[-1])
            return Response(200, fixture("details")[endpoint])
        query, variables = payload["query"], payload["variables"]
        if "MediaTagCollection" in query:
            self.tags_calls += 1
            return Response(200, {"data": {"MediaTagCollection": [{"id": 5101, "name": "Vymyšlený tag",
                              "description": "Vymyšlený popis tagu.", "category": "Test", "isAdult": False}]}})
        if "SiteStatistics" in query:
            return Response(200, {"data": {"SiteStatistics": {"anime": {"nodes": [{"count": len(self.rows)}]}}}})
        if re.search(r"a\d+: Media", query):
            aliases = re.findall(r"(a\d+): Media\(id:(\d+)\).*?recommendations\(page:(\d+)", query)
            if self.alias_fail and len(aliases) > 1:
                return Response(404)
            if any(int(ident) in self.missing for alias, ident, page in aliases):
                return Response(404)
            return Response(200, {"data": {alias: {"recommendations": copy.deepcopy(
                self.rec_pages.get((int(ident), int(page)), {"pageInfo": {"hasNextPage": False}, "nodes": []}))}
                for alias, ident, page in aliases}})
        page = variables.get("page", 1)
        if page > 100:
            return Response(400, {"errors": [{"message": "Vymyšlená chyba hloubky"}]})
        rows = self.rows
        if "UPDATED_AT_DESC" in query:
            rows = self.updated if self.updated is not None else sorted(rows, key=lambda row: -row["updatedAt"])
        elif "idMal_in" in query:
            rows = [row for row in rows if row.get("idMal") in variables["ids"]]
        else:
            rows = [row for row in rows if row["isAdult"] == variables["adult"]
                    and row["popularity"] > variables["greater"]
                    and (variables["lesser"] is None or row["popularity"] < variables["lesser"])]
            rows = sorted(rows, key=lambda row: row["id"], reverse="sort:ID_DESC" in query)
        start = (page - 1) * 50
        return Response(200, {"data": {"Page": {"pageInfo": {"total": 5000, "lastPage": 100,
                         "hasNextPage": start + 50 < len(rows)}, "media": copy.deepcopy(rows[start:start + 50])}}})

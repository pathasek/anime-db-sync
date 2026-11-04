"""AniList: obnovitelný snímek, bezpečné řezy a obnova malého okolí."""

from datetime import date
from pathlib import Path
from osobni import neighborhood
from tempo import Response, StopSource

BASE = "https://graphql.anilist.co"
ANILIST_SVOLENI_DO = date(2026, 11, 11)
RECS = """pageInfo { hasNextPage } nodes {
 rating mediaRecommendation { id idMal type }
}"""
CHARACTERS = """pageInfo { hasNextPage } edges { role
 node { id name { full native alternative } gender age favourites }
 voiceActors(sort: [RELEVANCE, ID]) { id name { full native } languageV2 } }"""
STAFF = """pageInfo { hasNextPage } edges { role node { id name { full native } } }"""
CONNECTIONS = {
    "recommendations": ("al_recs", "recs", "RATING_DESC", RECS),
    "characters": ("al_characters", "postavy", "[ROLE, RELEVANCE, ID]", CHARACTERS),
    "staff": ("al_staff", "stab", "[RELEVANCE, ID]", STAFF),
}
FIELDS = """id idMal type format status(version: 2) source(version: 3) countryOfOrigin isAdult isLicensed
 title { romaji english native } synonyms description(asHtml: false)
 startDate { year month day } endDate { year month day } season seasonYear episodes duration
 genres averageScore meanScore popularity favourites trending hashtag updatedAt
 coverImage { extraLarge color } bannerImage trailer { id site }
 nextAiringEpisode { airingAt episode }
 rankings { rank type format year season allTime context }
 stats { scoreDistribution { score amount } statusDistribution { status amount } }
 studios { edges { isMain node { id name isAnimationStudio } } }
 externalLinks { site url type language }
 streamingEpisodes { title thumbnail url site }
 tags { id rank isMediaSpoiler }
 recommendations(perPage: 25, sort: RATING_DESC) { """ + RECS + """ }
 characters(perPage: 25, sort: [ROLE, RELEVANCE, ID]) { """ + CHARACTERS + """ }
 staff(perPage: 25, sort: [RELEVANCE, ID]) { """ + STAFF + """ }
 relations { edges { relationType(version: 2) node { id idMal type format } } }
"""
BOUNDS = ((0, 0), (1, 9), (10, 49), (50, 199), (200, 999),
          (1000, 4999), (5000, 19999), (20000, 99999), (100000, None))


def recommendation_rows(connection):
    return [[target["id"], target.get("idMal"), row.get("rating", 0)]
            for row in (connection or {}).get("nodes", [])
            if row and (target := row.get("mediaRecommendation")) and target.get("type") == "ANIME"]


def compact(media):
    result = {key: value for key, value in media.items()
              if key not in ("tags", "recommendations", "relations", "characters", "staff", "studios")}
    result["tags"] = [[tag["id"], tag.get("rank", 0), int(bool(tag.get("isMediaSpoiler")))]
                      for tag in media.get("tags", [])]
    result["recommendations"] = recommendation_rows(media.get("recommendations"))
    result["relations"] = [[edge["relationType"], node["id"], node.get("idMal"),
                            node.get("type"), node.get("format")]
                           for edge in media.get("relations", {}).get("edges", [])
                           if (node := edge.get("node"))]
    for name in ("characters", "staff"):
        result[name] = connection_rows(name, media.get(name))
    result["studios"] = [[node["id"], node.get("name"), edge.get("isMain"), node.get("isAnimationStudio")]
                         for edge in (media.get("studios") or {}).get("edges", [])
                         if (node := edge.get("node"))]
    return result


def connection_rows(name, connection):
    if name == "recommendations":
        return recommendation_rows(connection)
    result = []
    for edge in (connection or {}).get("edges", []):
        node = edge["node"]
        person = node.get("name") or {}
        row = [node["id"], edge.get("role"), person.get("full"), person.get("native")]
        if name == "characters":
            row.extend([person.get("alternative") or [], node.get("gender"), node.get("age"),
                        node.get("favourites"),
                        [[actor["id"], (actor.get("name") or {}).get("full"),
                          (actor.get("name") or {}).get("native"), actor.get("languageV2")]
                         for actor in edge.get("voiceActors", [])]])
        result.append(row)
    return result


def new_slice(adult, low, high):
    return {"adult": adult, "lo": low, "hi": high, "page": 1, "reverse": False, "pivot": 0,
            "observed_lo": None, "observed_hi": None}


def metadata(al, storage):
    """Počet a datum odpovídají aktuálnímu režimu, původní snímek zůstává ve stavu."""
    snap = al.get("snapshot", {})
    mode = al.get("mode", "cele" if snap else "okoli")
    nearby = set(al.get("neighborhood", []))
    count = (snap.get("count", len(snap["ids"]) if "ids" in snap else
                      sum(1 for _ in storage.items("anilist", "media"))) if mode == "cele" else
             sum(row.get("idMal") in nearby for ident, row in storage.items("anilist", "media")))
    return {"datum": (snap.get("completed", snap.get("date", al.get("sample_date"))) if mode == "cele" else
                      al.get("neighborhood_date")), "pocet": count, "celkem": snap.get("total"),
            "rozdil": snap.get("difference"), "rezim": mode, "hotovo": bool(snap.get("done")),
            "neuplne_rezy": len(snap.get("incomplete", []))}


class AniList:
    def __init__(self, client, state, storage, today, deadline, personal=None, wall_now=None, log=print,
                 today_fn=None, sample_ids=None):
        self.client, self.state, self.storage = client, state, storage
        self.today, self.deadline, self.personal = today, deadline, personal
        self.wall_now = wall_now or (lambda: __import__("time").time())
        self.log = log
        self.today_fn = today_fn or (lambda: self.today)
        self.bulk = False
        self.sample_ids = sample_ids
        self._nearby_key = None
        self.al = state.data["anilist"]
        self.al.setdefault("pending", {})
        # Starší stav doporučení měl podobu {médium: strana}.
        for ident, value in list(self.al["pending"].items()):
            if isinstance(value, int):
                self.al["pending"][ident] = {"recommendations": value}
        self.al.setdefault("snapshot", {})
        self.snapshot = self.al["snapshot"]
        self.seen = set(self.snapshot.get("ids", []))

    def query(self, endpoint, query, variables=None):
        if self.bulk and self.today_fn() >= ANILIST_SVOLENI_DO:
            raise StopSource("Svolení ke snímku skončilo")
        # Vadná odpověď se jen zaznamená. Den nezamyká: jedna stránka nesmí
        # zastavit zbytek snímku před koncem svolení.
        response = self.client.request(endpoint, BASE, {"query": query, "variables": variables or {}},
                                       self.deadline)
        if response.status == 200 and endpoint in ("snimek", "zmeny", "okoli", "vzorek"):
            try:
                page = response.body["data"]["Page"]
                if not isinstance(page["media"], list) or not isinstance(page.get("pageInfo", {}), dict):
                    raise TypeError("Neplatná stránka")
                if type(page.get("pageInfo", {}).get("hasNextPage", False)) is not bool:
                    raise TypeError("Neplatné stránkování")
            except Exception:
                self.state.failure("al_odpoved", endpoint, self.today, 0)
                return Response(0)
        if response.status == 200 and self.state.get("al_odpoved", endpoint)[1]:
            self.state.success("al_odpoved", endpoint, self.today)
        return response

    def save_media(self, media, snapshot=False):
        ident = media.get("id") if isinstance(media, dict) else None
        if type(ident) is not int or ident <= 0:
            self.state.failure("anilist", "neplatne", self.today, 0)
            return False
        if snapshot and ident in self.seen:
            return True
        if not self.state.eligible("anilist", ident, self.today, age=0):
            return False
        try:
            reduced = compact(media)
            progress = {name: 2 for name in CONNECTIONS
                        if (media.get(name) or {}).get("pageInfo", {}).get("hasNextPage")}
            self.storage.put("anilist", "media", ident, reduced)
        except Exception:
            self.state.failure("anilist", ident, self.today, 0)
            if isinstance(media.get("idMal"), int):
                self.state.failure("al_mal", media["idMal"], self.today, 0)
            return False
        self.state.success("anilist", ident, self.today)
        if media.get("idMal") is not None:
            self.state.success("al_mal", media["idMal"], self.today)
        pending = self.al["pending"]
        if progress:
            pending[str(ident)] = progress
            for name in progress:
                self.state.data["items"].get(CONNECTIONS[name][0], {}).pop(str(ident), None)
        else:
            pending.pop(str(ident), None)
        if snapshot:
            self.seen.add(ident)
            self.snapshot.setdefault("ids", []).append(ident)
        return True

    def nearby(self):
        paths = list((self.storage.root / "tenrai").glob("*/*.jsonl.gz"))
        if self.personal:
            paths.extend(Path(self.personal) / name
                         for name in ("anime_list.json", "plan_to_watch.json"))
        fingerprint = []
        for path in paths:
            try:
                stat = path.stat()
                fingerprint.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                pass
        key = (self.storage.revision if self.storage.source != "anilist" else 0,
               tuple(sorted(fingerprint)))
        if key != self._nearby_key:
            self.al["neighborhood"] = sorted(neighborhood(self.storage, self.personal))
            self._nearby_key = key
        return set(self.al["neighborhood"])

    def tags(self, snapshot=False):
        day = self.today.toordinal()
        last = self.al.get("tags_day", 0)
        if snapshot and self.snapshot.get("tags_done"):
            return True
        if not snapshot and day - last < 30:
            return True
        if not self.state.eligible("al_tags", 0, self.today, age=0):
            return True
        response = self.query("tagy", "{ MediaTagCollection { id name description category rank "
                              "isGeneralSpoiler isMediaSpoiler isAdult } }")
        if response.status != 200:
            self.state.failure("al_tags", 0, self.today, response.status)
            return False
        try:
            tags = response.body["data"]["MediaTagCollection"]
            if not isinstance(tags, list):
                raise TypeError("Neplatné tagy")
            self.storage.write_json("anilist", "tagy", {str(tag["id"]): tag for tag in tags})
        except Exception:
            self.state.failure("al_tags", 0, self.today, 0)
            return False
        self.state.success("al_tags", 0, self.today)
        self.al["tags_day"] = day
        if snapshot:
            self.snapshot["tags_done"] = True
        return True

    def run(self):
        if self.sample_ids is not None:
            self.run_sample()
        elif self.today < ANILIST_SVOLENI_DO and not self.snapshot.get("done"):
            self.run_snapshot()
        else:
            self.run_neighborhood()

    def run_snapshot(self):
        self.bulk = True
        self.al["mode"] = "cele"
        snap = self.snapshot
        if not snap:
            snap.update(done=False, date=self.today.isoformat(), ids=[], incomplete=[],
                        todo=[new_slice(adult, lo, hi) for adult in (False, True) for lo, hi in BOUNDS])
        if not self.tags(snapshot=True):
            return
        # Řez, jehož stránka selhala, jde na konec fronty a ostatní pokračují.
        # Když dnes selhávají všechny zbylé řezy nebo chyb je moc, řezy končí
        # a noc pokračuje dostahováním už uložených médií.
        failures = skipped = 0
        while snap["todo"] and failures < 20 and skipped < len(snap["todo"]):
            part = snap["todo"][0]
            key = f"{int(part['adult'])}:{part['lo']}:{part['hi']}:{part['page']}:{int(part['reverse'])}"
            if not self.state.eligible("al_pages", key, self.today, age=0):
                snap["todo"].append(snap["todo"].pop(0))
                skipped += 1
                continue
            skipped = 0
            # Ostré nerovnosti na API odpovídají zdejší uzavřené celočíselné mezi.
            query = ("query($page:Int,$adult:Boolean,$greater:Int,$lesser:Int){"
                     "Page(page:$page,perPage:50){pageInfo{hasNextPage}"
                     "media(type:ANIME,sort:" + ("ID_DESC" if part["reverse"] else "ID") +
                     ",isAdult:$adult,popularity_greater:$greater,popularity_lesser:$lesser){" + FIELDS + "}}}")
            variables = {"page": part["page"], "adult": part["adult"], "greater": part["lo"] - 1}
            # Otevřená horní mez: na explicitní null AniList odpoví HTTP 400,
            # proměnná proto v dotazu úplně chybí.
            if part["hi"] is not None:
                variables["lesser"] = part["hi"] + 1
            response = self.query("snimek", query, variables)
            if response.status != 200:
                self.state.failure("al_pages", key, self.today, response.status)
                failures += 1
                snap["todo"].append(snap["todo"].pop(0))
                continue
            try:
                page = response.body["data"]["Page"]
                rows = page["media"]
                more = page.get("pageInfo", {}).get("hasNextPage", False)
                if not isinstance(rows, list):
                    raise TypeError("Neplatná stránka")
            except Exception:
                self.state.failure("al_pages", key, self.today, 0)
                failures += 1
                snap["todo"].append(snap["todo"].pop(0))
                continue
            self.state.success("al_pages", key, self.today)
            valid_ids = []
            met = False
            for index, media in enumerate(rows):
                try:
                    ident = media["id"]
                    if type(ident) is not int:
                        raise TypeError("Neplatné médium")
                    valid_ids.append(ident)
                    met |= part["reverse"] and ident <= part["pivot"]
                    popularity = media.get("popularity") or 0
                    part["observed_lo"] = popularity if part["observed_lo"] is None else min(part["observed_lo"], popularity)
                    part["observed_hi"] = popularity if part["observed_hi"] is None else max(part["observed_hi"], popularity)
                    self.save_media(media, snapshot=True)
                except Exception:
                    ident = media.get("id") if isinstance(media, dict) else None
                    self.state.failure("anilist", ident if type(ident) is int else f"{key}:{index}", self.today, 0)
            more = page.get("pageInfo", {}).get("hasNextPage", False)
            if met or not more:
                snap["todo"].pop(0)
            elif part["page"] < 100:
                if not part["reverse"] and valid_ids:
                    part["pivot"] = max(part["pivot"], max(valid_ids))
                part["page"] += 1
            elif not part["reverse"]:
                if valid_ids:
                    part["pivot"] = max(part["pivot"], max(valid_ids))
                part.update(reverse=True, page=1)
            else:
                low, high = part["lo"], part["hi"]
                # U otevřené horní meze volíme pozorovanou hodnotu, obě poloviny
                # přesto pokrývají původní rozsah včetně nepozorovaného ocasu.
                cut_high = high if high is not None else part["observed_hi"]
                if high == low:
                    snap["incomplete"].append([part["adult"], low, high])
                    snap["todo"].pop(0)
                    self.log(f"anilist snimek: neúplných řezů {len(snap['incomplete'])}")
                else:
                    middle = (max(low * 2, low + 1) if high is None and (cut_high or low) <= low
                              else max(low, (low + max(low, cut_high or low)) // 2))
                    snap["todo"][:1] = [new_slice(part["adult"], low, middle),
                                         new_slice(part["adult"], middle + 1, high)]
        self.finish_recommendations()
        if snap["todo"] or self.al["pending"]:
            return
        response = self.query("pocet", "{ SiteStatistics { anime(sort: DATE_DESC, perPage: 1) { nodes { count } } } }")
        if response.status != 200:
            return
        try:
            total = response.body["data"]["SiteStatistics"]["anime"]["nodes"][0]["count"]
            if type(total) is not int:
                raise TypeError("Neplatný počet")
        except Exception:
            self.state.failure("al_odpoved", "pocet", self.today, 0)
            return
        snap.update(done=True, count=len(self.seen), total=total, difference=total - len(self.seen),
                    completed=self.today.isoformat())
        self.log(f"anilist snimek: staženo {len(self.seen)}, rozdíl {total - len(self.seen)}")
        self.write_meta("cele")

    def finish_recommendations(self, allowed=None):
        pending = self.al["pending"]
        for name, (endpoint, _, _, _) in CONNECTIONS.items():
            while True:
                ids = [int(ident) for ident in sorted(pending, key=int)
                       if name in pending[ident]
                       and (allowed is None or int(ident) in allowed)
                       and self.state.eligible(endpoint, ident, self.today, age=0)][:10]
                if not ids:
                    break
                self.recommendation_batch(ids, name)

    def drop_pending(self, ident, name):
        pending = self.al["pending"]
        pending[str(ident)].pop(name, None)
        if not pending[str(ident)]:
            pending.pop(str(ident))

    def recommendation_batch(self, ids, name="recommendations"):
        pending = self.al["pending"]
        endpoint, label, sort, fields = CONNECTIONS[name]
        query = "{" + " ".join(
            f"a{index}: Media(id:{ident}) {{ {name}(page:{pending[str(ident)][name]},"
            f"perPage:25,sort:{sort}){{" + fields + "} }"
            for index, ident in enumerate(ids)) + "}"
        response = self.query(label, query)
        nodes = response.body.get("data", {}) if response.status == 200 else {}
        # Chyba jednotlivého uzlu rovněž rozpadne vícečlennou dávku.
        invalid = response.status != 200 or any(
            not isinstance(nodes.get(f"a{index}"), dict)
            or not isinstance(nodes[f"a{index}"].get(name), dict)
            for index in range(len(ids)))
        if invalid and len(ids) > 1:
            for ident in ids:
                self.recommendation_batch([ident], name)
            return
        if response.status != 200:
            for ident in ids:
                self.state.failure(endpoint, ident, self.today, response.status)
                if response.status == 404:
                    self.drop_pending(ident, name)
            return
        prepared = []
        for index, ident in enumerate(ids):
            node = nodes.get(f"a{index}")
            if node is None:
                self.state.failure(endpoint, ident, self.today, 404)
                self.drop_pending(ident, name)
                continue
            try:
                connection = node[name]
                more = connection["pageInfo"]["hasNextPage"]
                if type(more) is not bool or not isinstance(
                        connection["nodes" if name == "recommendations" else "edges"], list):
                    raise TypeError("Neplatné spojení")
                rows = connection_rows(name, connection)
                media = dict(self.storage.get("anilist", "media", ident))
                # Jedna osoba může mít několik rolí, proto klíč obsahuje i roli.
                key = (lambda row: row[0]) if name == "recommendations" else (lambda row: (row[0], row[1]))
                merged = {key(row): row for row in media[name]}
                merged.update({key(row): row for row in rows})
                media[name] = list(merged.values())
                prepared.append((ident, media, more))
            except Exception:
                if len(ids) > 1:
                    for single in ids:
                        self.recommendation_batch([single], name)
                else:
                    self.state.failure(endpoint, ident, self.today, 0)
                return
        for ident, media, more in prepared:
            try:
                self.storage.put("anilist", "media", ident, media)
            except Exception:
                self.state.failure(endpoint, ident, self.today, 0)
                continue
            self.state.success(endpoint, ident, self.today)
            if more:
                pending[str(ident)][name] += 1
            else:
                self.drop_pending(ident, name)

    def fetch_mal(self, ids, endpoint="okoli"):
        for offset in range(0, len(ids), 50):
            batch = ids[offset:offset + 50]
            response = self.query(endpoint, "query($ids:[Int]){Page(perPage:50){"
                                  "media(type:ANIME,idMal_in:$ids){" + FIELDS + "}}}", {"ids": batch})
            if response.status != 200:
                for ident in batch:
                    self.state.failure("al_mal", ident, self.today, response.status)
                continue
            try:
                rows = response.body["data"]["Page"]["media"]
                if not isinstance(rows, list):
                    raise TypeError("Neplatná média")
            except Exception:
                for ident in batch:
                    self.state.failure("al_mal", ident, self.today, 0)
                continue
            failed, saved = set(), set()
            for index, media in enumerate(rows):
                mal = media.get("idMal") if isinstance(media, dict) else None
                try:
                    if self.save_media(media):
                        saved.add(mal)
                    else:
                        failed.add(mal)
                except Exception:
                    ident = media.get("id") if isinstance(media, dict) else None
                    self.state.failure("anilist", ident if type(ident) is int else f"{endpoint}:{offset}:{index}", self.today, 0)
                    failed.add(mal)
            # Vadné médium bez idMal nejde přiřadit. Neuspějí jen anime dávky,
            # která se platně neuložila, uložená zůstanou úspěšná.
            unknown = None in failed
            for ident in batch:
                if ident in failed or (unknown and ident not in saved):
                    self.state.failure("al_mal", ident, self.today, 0)
                else:
                    # Nenalezené idMal je platný prázdný výsledek.
                    self.state.success("al_mal", ident, self.today)
            changed = set(self.al.get("changed", []))
            changed.difference_update(set(batch) - failed)
            self.al["changed"] = sorted(changed)

    def run_sample(self):
        self.al["mode"] = "cele"
        self.al["sample_date"] = self.today.isoformat()
        self.tags()
        due = [ident for ident in sorted(self.sample_ids)
               if self.state.eligible("al_mal", ident, self.today)]
        self.fetch_mal(due, "vzorek")
        allowed = {ident for ident, row in self.storage.items("anilist", "media")
                   if row.get("idMal") in self.sample_ids}
        self.finish_recommendations(allowed)

    def track_changes(self, rows, ids, since):
        changed, older = set(), False
        for index, row in enumerate(rows):
            try:
                updated = row["updatedAt"]
                if type(updated) is not int:
                    raise TypeError("Neplatné datum změny")
                if row.get("idMal") in ids and updated >= since:
                    changed.add(row["idMal"])
                older |= updated < since
            except Exception:
                ident = row.get("id") if isinstance(row, dict) else None
                self.state.failure("anilist", ident if type(ident) is int else f"zmeny:{index}", self.today, 0)
                mal = row.get("idMal") if isinstance(row, dict) else None
                if type(mal) is int:
                    self.state.failure("al_mal", mal, self.today, 0)
        return changed, older

    def run_neighborhood(self):
        self.bulk = False
        self.al["mode"] = "okoli"
        self.al["neighborhood_date"] = self.today.isoformat()
        ids = self.nearby()
        self.tags()
        # Rozpracované sledování změn má vlastní pevnou horní časovou mez.
        # Po pádu se nesmí posunout kurzor za dosud nepřečtené stránky.
        scan = self.al.setdefault("updates", {"page": 1, "since": self.al.get("last_run", int(self.wall_now())),
                                               "until": int(self.wall_now())})
        changed = set(self.al.get("changed", []))
        while True:
            response = self.query("zmeny", "query($page:Int){Page(page:$page,perPage:50){"
                                  "pageInfo{hasNextPage} media(type:ANIME,sort:UPDATED_AT_DESC){id idMal updatedAt}}}",
                                  {"page": scan["page"]})
            if response.status != 200:
                return
            page = response.body["data"]["Page"]
            rows = page["media"]
            found, older = self.track_changes(rows, ids, scan["since"])
            changed.update(found)
            self.al["changed"] = sorted(changed)
            if (not rows or older
                    or not page.get("pageInfo", {}).get("hasNextPage")):
                self.al["last_run"] = scan["until"]
                self.al.pop("updates")
                break
            # Za veřejnou hloubku nelze pokračovat. Ověříme tedy metadata
            # pouze malé známé množiny okolí, abychom bezpečně posunuli kurzor.
            if scan["page"] == 100:
                candidates = sorted(ids)
                offset = scan.setdefault("fallback", 0)
                while offset < len(candidates):
                    batch = candidates[offset:offset + 50]
                    response = self.query("zmeny", "query($ids:[Int]){Page(perPage:50){"
                                          "media(type:ANIME,idMal_in:$ids){id idMal updatedAt}}}",
                                          {"ids": batch})
                    if response.status != 200:
                        return
                    found, _ = self.track_changes(response.body["data"]["Page"]["media"], ids, scan["since"])
                    changed.update(found)
                    self.al["changed"] = sorted(changed)
                    offset += len(batch)
                    scan["fallback"] = offset
                self.al["last_run"] = scan["until"]
                self.al.pop("updates")
                break
            scan["page"] += 1
        due = [ident for ident in sorted(ids) if
               (ident in changed or self.state.eligible("al_mal", ident, self.today, age=30))
               and self.state.get("al_mal", ident)[4] != self.today.toordinal()
               and self.state.get("al_mal", ident)[2] <= self.today.toordinal()]
        self.fetch_mal(due)
        allowed = {ident for ident, row in self.storage.items("anilist", "media") if row.get("idMal") in ids}
        self.finish_recommendations(allowed)
        self.write_meta("okoli")

    def write_meta(self, mode):
        self.al["mode"] = mode
        self.storage.flush()
        self.state.save()
        self.storage.write_json("anilist", "meta", metadata(self.al, self.storage))

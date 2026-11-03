"""Deterministické pořadí Tenrai: nové, aktivní, první průchod, rotace."""

import tenrai
from tempo import StopSource

AGES = {"list": 7, "recs": 30, "stats": 30, "full": 90, "staff": 90, "chars": 90}


class Queue:
    def __init__(self, state, storage, today):
        self.state, self.storage, self.today = state, storage, today

    def catalog(self):
        return {ident: row for ident, row in self.storage.items("tenrai", "list")
                if not self.state.gone(ident)}

    def initial(self):
        anime = self.catalog()
        ordered = sorted(anime, key=lambda ident: (-(anime[ident].get("members") or 0), ident))
        for endpoint in tenrai.ENDPOINTS[1:]:
            for ident in ordered:
                if self.state.eligible(endpoint, ident, self.today):
                    yield endpoint, ident

    def rotation(self):
        anime = self.catalog()
        tasks = []
        for page in range(1, self.state.data.get("list_pages", 0) + 1):
            item = self.state.get("list", f"asc:{page}", pages=True)
            if self.state.eligible("list", f"asc:{page}", self.today, 7, pages=True):
                tasks.append((item[0], 0, page, "list"))
        for endpoint in tenrai.ENDPOINTS[1:]:
            for ident in anime:
                item = self.state.get(endpoint, ident)
                if item[0] and self.state.eligible(endpoint, ident, self.today, AGES[endpoint]):
                    tasks.append((item[0], tenrai.ENDPOINTS.index(endpoint), ident, endpoint))
        for _, _, ident, endpoint in sorted(tasks):
            yield endpoint, ident

    def first_done(self):
        if not self.state.data.get("list_complete"):
            return False
        return all(self.state.get(endpoint, ident)[0] or self.state.gone(ident)
                   for endpoint in tenrai.ENDPOINTS[1:] for ident in self.catalog())


class Tenrai:
    def __init__(self, client, state, storage, today, deadline, sample=None):
        self.client, self.state, self.storage = client, state, storage
        self.today, self.deadline = today, deadline
        self.sample = sample
        self.queue = Queue(state, storage, today)
        night = state.data.setdefault("night", {})
        if night.get("date") != today.isoformat():
            night.clear()
            night.update(date=today.isoformat(), scans={},
                         known_max=max((ident for ident, _ in storage.items("tenrai", "list")), default=0),
                         new=[])
        self.night = night

    def fetch_item(self, endpoint, ident, age=None):
        if self.state.gone(ident) or not self.state.eligible(endpoint, ident, self.today, age):
            return
        try:
            response = self.client.request(endpoint, tenrai.url(endpoint, ident), deadline=self.deadline)
        except StopSource as error:
            if error.status is not None:
                self.state.failure(endpoint, ident, self.today, error.status)
            raise
        if response.status == 200:
            try:
                self.storage.put("tenrai", endpoint, ident, tenrai.reduce(endpoint, response.body["data"]))
            except Exception:
                self.state.failure(endpoint, ident, self.today, 0)
            else:
                self.state.success(endpoint, ident, self.today)
        else:
            self.state.failure(endpoint, ident, self.today, response.status)

    def page(self, number, mode):
        key = f"{mode}:{number}"
        if not self.state.eligible("list", key, self.today, age=0, pages=True):
            return None
        try:
            response = self.client.request("list", tenrai.url(page=number, mode=mode), deadline=self.deadline)
        except StopSource as error:
            if error.status is not None:
                self.state.failure("list", key, self.today, error.status, pages=True)
            raise
        if response.status == 200:
            try:
                body = response.body
                if not isinstance(body["data"], list) or not isinstance(body.get("pagination", {}), dict):
                    raise TypeError("Neplatná stránka")
                if type(body.get("pagination", {}).get("last_visible_page", 0)) is not int:
                    raise TypeError("Neplatné stránkování")
            except Exception:
                self.state.failure("list", key, self.today, 0, pages=True)
                return None
            if mode == "asc":
                self.state.data["list_pages"] = max(self.state.data.get("list_pages", 0), number,
                    number + int(bool(body.get("pagination", {}).get("has_next_page"))),
                    response.body.get("pagination", {}).get("last_visible_page", 0))
                self.state.data.setdefault("list_page_ids", {})[str(number)] = [
                    row["mal_id"] for row in body["data"]
                    if isinstance(row, dict) and type(row.get("mal_id")) is int and row["mal_id"] > 0]
            valid_page = True
            for row in response.body["data"]:
                ident = row.get("mal_id") if isinstance(row, dict) else None
                if type(ident) is not int or ident <= 0:
                    valid_page = False
                    continue
                try:
                    if not self.state.gone(ident):
                        self.storage.put("tenrai", "list", ident, tenrai.reduce("list", row))
                        self.state.success("list", ident, self.today)
                except Exception:
                    self.state.failure("list", ident, self.today, 0)
            if valid_page:
                self.state.success("list", key, self.today, pages=True)
            else:
                self.state.failure("list", key, self.today, 0, pages=True)
            return response.body
        self.state.failure("list", key, self.today, response.status, pages=True)
        if response.status == 400:
            return {"data": [], "pagination": {"has_next_page": False}, "_status": 400}
        return None

    def scan(self, mode):
        scan = self.night["scans"].setdefault(mode, {"page": 1, "done": False})
        while not scan["done"]:
            body = self.page(scan["page"], mode)
            if body is None:
                return
            rows = [row for row in body["data"] if isinstance(row, dict) and type(row.get("mal_id")) is int]
            if mode == "desc":
                new = set(self.night["new"])
                new.update(row["mal_id"] for row in rows if row["mal_id"] > self.night["known_max"])
                self.night["new"] = sorted(new)
            known = mode == "desc" and any(row["mal_id"] <= self.night["known_max"] for row in rows)
            if known or not body.get("pagination", {}).get("has_next_page") or not rows:
                scan["done"] = True
            else:
                scan["page"] += 1

    def initial_list(self):
        last = self.state.data.get("list_last")
        page = 1
        while last is None or page <= last:
            key = f"asc:{page}"
            record = self.state.get("list", key, pages=True)
            if not record[0] and self.state.eligible("list", key, self.today, pages=True):
                body = self.page(page, "asc")
                if body is not None:
                    advertised = body.get("pagination", {}).get("last_visible_page")
                    if type(advertised) is int and advertised > 0:
                        last = advertised
                        self.state.data["list_last"] = last
                    missing = body.get("_status") == 400
                    if missing and page <= self.state.data.get("list_pages", 0):
                        # Z dřívější odpovědi víme, že tato stránka existuje.
                        # Její chyba tedy nemůže ukončit zbytek seznamu.
                        pass
                    elif not body.get("pagination", {}).get("has_next_page") or not body["data"]:
                        last = page if body["data"] else page - 1
                        self.state.data["list_last"] = last
                        self.state.data["list_pages"] = last
            page += 1
        self.state.data["list_complete"] = all(
            (record := self.state.get("list", f"asc:{number}", pages=True))[0]
            or record[2] > self.today.toordinal() or record[3]
            for number in range(1, last + 1))

    def run_sample(self):
        selected = set()
        for page in range(1, (self.sample + 24) // 25 + 1):
            self.page(page, "asc")
        # Při obnovení použijeme i již uložené stránky vzorku.
        selected.update(ident for page in range(1, (self.sample + 24) // 25 + 1)
                        for ident in self.state.data.get("list_page_ids", {}).get(str(page), []))
        for endpoint in tenrai.ENDPOINTS[1:]:
            for ident in sorted(selected):
                self.fetch_item(endpoint, ident)

    def run(self):
        if self.sample is not None:
            self.run_sample()
            return
        if self.night["known_max"]:
            self.scan("desc")
        # Priorita nových anime je nezávislá na rozpracovaném prvním průchodu.
        if self.night["known_max"]:
            for endpoint in tenrai.ENDPOINTS[1:]:
                for ident in self.night["new"]:
                    self.fetch_item(endpoint, ident)
        if self.night["known_max"]:
            self.scan("airing")
            self.scan("upcoming")
        self.initial_list()
        catalog = self.queue.catalog()
        active = sorted(ident for ident, row in catalog.items()
                        if row.get("status") in ("Currently Airing", "Not yet aired"))
        for endpoint in ("recs", "stats"):
            for ident in active:
                age = 1 if catalog[ident].get("status") == "Currently Airing" else 7
                self.fetch_item(endpoint, ident, age=age)
        for endpoint, ident in self.queue.initial():
            self.fetch_item(endpoint, ident)
        if self.queue.first_done():
            list_finished = False
            for endpoint, ident in self.queue.rotation():
                if endpoint == "list":
                    if not list_finished:
                        body = self.page(ident, "asc")
                        list_finished = body is not None and not body.get("pagination", {}).get("has_next_page")
                else:
                    self.fetch_item(endpoint, ident, AGES[endpoint])

    def remaining(self):
        return sum(1 for _ in self.queue.initial()) + sum(1 for _ in self.queue.rotation())

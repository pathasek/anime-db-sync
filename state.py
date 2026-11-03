"""Kompaktní stav, atomické zápisy a kalendář splatnosti."""

import json
import os
from datetime import date
from pathlib import Path
import tempfile
import time


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def atomic_write(path, content):
    """Nahrazení je atomické; shodný obsah zachová i čas souboru."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return False
    fd, temporary = tempfile.mkstemp(prefix=".zapis-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def replace(source, target):
    """Krátké sdílené zámky Windows (indexace či synchronizace) mohou pominout."""
    for pause in (0.02, 0.05, 0.1, 0.2, 0.5, None):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if pause is None:
                raise
            time.sleep(pause)


class State:
    """Záznam: [den úspěchu, chybné noci, přeskočit do, pryč, den chyby]."""

    def __init__(self, directory, source="tenrai"):
        if source not in ("tenrai", "anilist"):
            raise ValueError("Neplatný zdroj stavu")
        self.source = source
        self.directory = Path(directory)
        self.path = self.directory / ("anilist-state.json" if source == "anilist" else "state.json")
        legacy = self.directory / "state.json"
        origin = self.path if self.path.exists() else legacy if source == "anilist" and legacy.exists() else None
        loaded = json.loads(origin.read_text(encoding="utf-8")) if origin else {"v": 1, "items": {}}
        self.data = {"v": loaded.get("v"), "items": {
            key: value for key, value in loaded.get("items", {}).items()
            if (key == "anilist" or key.startswith("al_")) == (source == "anilist")}}
        if source == "anilist":
            self.data["anilist"] = loaded.get("anilist", {})
        else:
            self.data.update({key: value for key, value in loaded.items() if key not in ("v", "items", "anilist")})
            self.data.setdefault("pages", {})
            self.data.setdefault("tenrai_days", {})
        if self.data.get("v") != 1:
            raise ValueError("Nepodporovaná verze stavu")
        # Malý žurnál chrání denní strop i při tvrdém ukončení mezi checkpointy.
        self.counter_path = self.directory / "tenrai-den.json"
        if source == "tenrai" and self.counter_path.exists():
            journal = json.loads(self.counter_path.read_text(encoding="utf-8"))
            for day, count in journal.get("days", journal).items():
                self.data["tenrai_days"][day] = max(count, self.data["tenrai_days"].get(day, 0))
            self.data["tenrai_last_start"] = journal.get("last_start", self.data.get("tenrai_last_start"))

    def save(self):
        atomic_write(self.path, json_bytes(self.data))

    def count_attempt(self, day, timestamp=None):
        if self.source != "tenrai":
            raise ValueError("Žurnál patří pouze Tenrai")
        days = self.data["tenrai_days"]
        key = day.isoformat()
        days[key] = days.get(key, 0) + 1
        self.data["tenrai_last_start"] = timestamp
        atomic_write(self.counter_path, json_bytes({"days": days, "last_start": timestamp}))

    def record(self, endpoint, ident, pages=False):
        group = self.data["pages"] if pages else self.data["items"].setdefault(endpoint, {})
        return group.setdefault(str(ident), [0, 0, 0, 0, 0])

    def get(self, endpoint, ident, pages=False):
        group = self.data["pages"] if pages else self.data["items"].get(endpoint, {})
        return group.get(str(ident), [0, 0, 0, 0, 0])

    def gone(self, ident):
        return any(items.get(str(ident), [0, 0, 0, 0])[3]
                   for endpoint, items in self.data["items"].items()
                   if endpoint in ("list", "recs", "full", "staff", "chars", "stats"))

    def success(self, endpoint, ident, today, pages=False):
        self.record(endpoint, ident, pages)[:] = [today.toordinal(), 0, 0, 0, 0]

    def failure(self, endpoint, ident, today, status=0, pages=False):
        item = self.record(endpoint, ident, pages)
        day = today.toordinal()
        if item[4] != day:
            item[1] += 1
            item[4] = day
            if item[1] >= 5:
                item[2] = day + 14
        if status == 404:
            item[3] = 1

    def eligible(self, endpoint, ident, today, age=None, pages=False):
        item = self.get(endpoint, ident, pages)
        day = today.toordinal()
        if item[3] or item[2] > day or item[4] == day:
            return False
        return not item[0] or (age is not None and day - item[0] >= age)

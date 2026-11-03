"""Líné blokové úložiště bez časových razítek v datech."""

import gzip
import io
import json
from pathlib import Path
from state import atomic_write, json_bytes

MAX_BYTES = 50 * 1024 * 1024


class Storage:
    def __init__(self, directory, source=None):
        self.root = Path(directory) / "raw"
        self.source = source
        self.revision = 0
        self.cache = {}
        self.dirty = set()

    def key(self, source, endpoint, ident):
        return source, endpoint, int(ident) // (10000 if source == "anilist" else 1000)

    def path(self, key):
        source, endpoint, block = key
        return self.root / source / endpoint / f"{block:04d}.jsonl.gz"

    def load(self, key):
        foreign = self.source is not None and key[0] != self.source
        if key not in self.cache or foreign:
            path = self.path(key)
            rows = {}
            try:
                if path.exists():
                    with gzip.open(path, "rt", encoding="utf-8") as stream:
                        for line in stream:
                            item = json.loads(line)
                            if foreign and (type(item["id"]) is not int or
                                    (key[1] == "list" and not isinstance(item["d"], dict)) or
                                    (key[1] == "recs" and not isinstance(item["d"], list))):
                                raise ValueError("Neplatný cizí blok")
                            rows[item["id"]] = item["d"]
            except (OSError, EOFError, ValueError, KeyError, TypeError):
                if not foreign:
                    raise
                rows = {}
            self.cache[key] = rows
        return self.cache[key]

    def put(self, source, endpoint, ident, data):
        self.check_owner(source)
        key = self.key(source, endpoint, ident)
        rows = self.load(key)
        if ident not in rows or rows[ident] != data:
            rows[ident] = data
            self.dirty.add(key)
            self.revision += 1

    def check_owner(self, source):
        if self.source is not None and source != self.source:
            raise ValueError("Zápis patří jinému zdroji")

    def get(self, source, endpoint, ident, default=None):
        return self.load(self.key(source, endpoint, ident)).get(ident, default)

    def items(self, source, endpoint):
        directory = self.root / source / endpoint
        keys = {(source, endpoint, int(path.name.split(".")[0]))
                for path in directory.glob("*.jsonl.gz")}
        keys.update(key for key in self.cache if key[:2] == (source, endpoint))
        for key in sorted(keys):
            yield from sorted(self.load(key).items())

    def flush(self):
        for key in sorted(self.dirty.copy()):
            self.check_owner(key[0])
            buffer = io.BytesIO()
            with gzip.GzipFile(filename="", mode="wb", fileobj=buffer,
                               compresslevel=6, mtime=0) as stream:
                for ident, data in sorted(self.load(key).items()):
                    stream.write(json_bytes({"id": ident, "d": data}) + b"\n")
            content = buffer.getvalue()
            if len(content) > MAX_BYTES:
                raise ValueError("Blok přesahuje povolených 50 MB")
            atomic_write(self.path(key), content)
            self.dirty.remove(key)

    def read_json(self, source, name, default=None):
        path = self.root / source / (name + ".json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def write_json(self, source, name, data):
        self.check_owner(source)
        content = json_bytes(data)
        if len(content) > MAX_BYTES:
            raise ValueError("Soubor přesahuje povolených 50 MB")
        atomic_write(self.root / source / (name + ".json"), content)

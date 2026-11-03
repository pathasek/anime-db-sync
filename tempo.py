"""Oddělené omezovače, bezpečný HTTP transport a opakování dotazů."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import http.client
import socket
import time
import urllib.error
import urllib.request

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 "
              "AnimeListWeb/1.0 (+https://github.com/pathasek/anime-db-sync)")

RETRYABLE = (OSError, urllib.error.URLError, http.client.HTTPException,
             json.JSONDecodeError, UnicodeDecodeError)


class StopSource(Exception):
    """Zdroj již v této noci nemůže pokračovat."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class StopNight(Exception):
    """Vypršel společný rozpočet času nebo dotazů."""


@dataclass
class Response:
    status: int
    body: object = None
    headers: dict = field(default_factory=dict)


def transport(method, url, headers, payload, timeout):
    """Jediné místo se sítí; HTTP chyby nikdy nečtou tělo odpovědi."""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as result:
            return Response(result.status, json.load(result), dict(result.headers))
    except urllib.error.HTTPError as error:
        error.close()
        return Response(error.code, headers=dict(error.headers))
    except RETRYABLE:
        return Response(599)


class Limiter:
    def __init__(self, rate, now=time.monotonic, sleep=time.sleep,
                 state=None, utcnow=None, daily_limit=36000):
        if rate <= 0:
            raise ValueError("Tempo musí být kladné")
        self.interval = 60 / rate
        self.now, self.sleep, self.state = now, sleep, state
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.daily_limit = daily_limit
        self.last = None
        self.stopped = False
        self.consecutive_429 = 0

    def wait(self, seconds, deadline):
        if self.now() + seconds >= deadline:
            raise StopSource("Vypršel čas zdroje")
        if seconds > 0:
            self.sleep(seconds)

    def start(self, deadline=float("inf")):
        if self.stopped:
            raise StopSource("Zdroj ukončen")
        delay = 0 if self.last is None else max(0, self.last + self.interval - self.now())
        if self.last is None and self.state and self.state.data.get("tenrai_last_start") is not None:
            delay = max(delay, min(self.interval,
                        self.state.data["tenrai_last_start"] + self.interval - self.utcnow().timestamp()))
        self.wait(delay, deadline)
        if self.state:
            day = self.utcnow().date()
            if self.state.data["tenrai_days"].get(day.isoformat(), 0) >= self.daily_limit:
                self.stopped = True
                raise StopSource("Denní strop dotazů")
            self.state.count_attempt(day, self.utcnow().timestamp())
        self.last = self.now()

    def limited(self, headers, deadline):
        self.consecutive_429 += 1
        if self.consecutive_429 >= 3:
            self.stopped = True
            raise StopSource("Třetí omezení zdroje", status=429)
        retry = next((v for k, v in headers.items() if k.lower() == "retry-after"), 0)
        try:
            retry = float(retry)
        except (TypeError, ValueError):
            try:
                retry = (parsedate_to_datetime(retry) - self.utcnow()).total_seconds()
            except (TypeError, ValueError, OverflowError):
                retry = 0
        self.wait(min(300, max(30 * self.consecutive_429, retry)), deadline)


class Client:
    def __init__(self, source, limiter, send=transport, before=None, after=None, log=print):
        self.source, self.limiter, self.send = source, limiter, send
        self.before, self.after, self.log = before, after, log
        self.counts = {}

    def request(self, endpoint, url, payload=None, deadline=float("inf")):
        failures = 0
        counts = self.counts.setdefault(endpoint, {"dotazu": 0, "stazeno": 0, "chyb": 0, "pryc": 0})
        while True:
            if self.before:
                self.before()
            self.limiter.start(deadline)
            counts["dotazu"] += 1
            try:
                headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
                if payload is not None:
                    headers["Content-Type"] = "application/json"
                try:
                    response = self.send("POST" if payload is not None else "GET", url,
                                         headers, payload, 30)
                except RETRYABLE:
                    response = Response(599)
            finally:
                # Počítá se i pokus, který skončil neočekávanou výjimkou transportu.
                if self.after:
                    self.after()
            status = response.status
            if status == 200 and (not isinstance(response.body, dict) or
                    (self.source == "anilist" and not isinstance(response.body.get("data"), dict)
                     and not response.body.get("errors"))):
                status = 502
            if status == 200 and isinstance(response.body, dict) and response.body.get("errors"):
                errors = response.body["errors"]
                try:
                    status = int(errors[0].get("status") or 400)
                except (TypeError, ValueError, IndexError, KeyError, AttributeError):
                    status = 400
            if status == 200:
                self.limiter.consecutive_429 = 0
                counts["stazeno"] += 1
                return response
            counts["chyb"] += 1
            if status == 404:
                counts["pryc"] += 1
            # Endpoint je interní konstanta, nikdy adresa s id ani tělo chyby.
            self.log(f"{self.source} {endpoint}: HTTP {status}")
            if status == 429:
                try:
                    self.limiter.limited(response.headers, deadline)
                except StopSource as error:
                    error.status = status
                    raise
                continue
            if 500 <= status <= 599 and failures < 2:
                try:
                    self.limiter.wait((10, 30)[failures], deadline)
                except StopSource as error:
                    error.status = status
                    raise
                failures += 1
                continue
            return Response(status)

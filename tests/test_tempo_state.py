"""Omezovače a stav bez skutečného čekání či přístupu na síť."""

from datetime import date, timedelta
from pathlib import Path
from tests.helpers import TemporaryDirectory
import unittest

from state import State
from tempo import Client, Limiter, Response, StopSource
from tests.helpers import Clock


class TempoTests(unittest.TestCase):
    def test_rozestupy_a_okna(self):
        for rate in (108, 20):
            clock = Clock()
            limiter = Limiter(rate, clock.now, clock.sleep)
            starts = []
            for _ in range(240):
                limiter.start()
                starts.append(clock.now())
            self.assertTrue(all(b - a >= 60 / rate - 1e-10 for a, b in zip(starts, starts[1:])))
            self.assertTrue(all(sum(t <= value < t + 1 for value in starts) <= 3 for t in starts))
            if rate == 20:
                self.assertEqual(starts[1], 3)

    def test_denni_strop_prezije_dva_behy_i_neulozeny_stav(self):
        with TemporaryDirectory() as folder:
            clock = Clock()
            first = State(folder)
            limiter = Limiter(108, clock.now, clock.sleep, first, clock.utcnow, daily_limit=3)
            limiter.start()
            first.save()
            limiter.start()
            # Druhý pokus je zatím pouze v žurnálu, jako při tvrdém ukončení.
            second = State(folder)
            resumed = Limiter(108, clock.now, clock.sleep, second, clock.utcnow, daily_limit=3)
            resumed.start()
            with self.assertRaises(StopSource):
                resumed.start()
            self.assertEqual(second.data["tenrai_days"]["2026-11-03"], 3)
            clock.value += 86400
            next_day = Limiter(108, clock.now, clock.sleep, State(folder), clock.utcnow, daily_limit=3)
            next_day.start()
            self.assertEqual(next_day.state.data["tenrai_days"]["2026-11-04"], 1)

    def client(self, responses):
        clock = Clock()
        iterator = iter(responses)

        def send(*args):
            item = next(iterator)
            if isinstance(item, BaseException):
                raise item
            return item

        limiter = Limiter(108, clock.now, clock.sleep, utcnow=clock.utcnow)
        return clock, limiter, Client("tenrai", limiter, send, log=lambda line: None)

    def test_tri_429(self):
        clock, limiter, client = self.client([Response(429)] * 3)
        with self.assertRaises(StopSource):
            client.request("stats", "https://priklad.invalid")
        self.assertEqual(clock.sleeps, [30, 60])
        self.assertTrue(limiter.stopped)
        self.assertEqual(client.counts["stats"]["dotazu"], 3)

    def test_retry_after_reset_a_oddeleni(self):
        clock, limiter, client = self.client([Response(429, headers={"Retry-After": "80"}),
                                              Response(200, {}), Response(429), Response(200, {})])
        client.request("stats", "https://priklad.invalid")
        client.request("stats", "https://priklad.invalid")
        self.assertIn(80, clock.sleeps)
        self.assertIn(30, clock.sleeps)
        self.assertEqual(limiter.consecutive_429, 0)
        other = Limiter(20, clock.now, clock.sleep)
        self.assertFalse(other.stopped)

    def test_retry_after_strop_a_http_datum(self):
        clock, _, client = self.client([Response(429, headers={"retry-after": "900"}), Response(200, {})])
        client.request("list", "https://priklad.invalid")
        self.assertEqual(clock.sleeps, [300])
        clock, _, client = self.client([Response(429, headers={"Retry-After": "Tue, 03 Nov 2026 00:02:00 GMT"}),
                                       Response(200, {})])
        client.request("list", "https://priklad.invalid")
        self.assertEqual(clock.sleeps, [120])

    def test_5xx_timeout_spojeni_dva_pokusy_navic(self):
        for failure in (Response(503), TimeoutError("TAJNY_TITUL"), ConnectionError("TAJNY_TITUL")):
            clock, _, client = self.client([failure] * 3)
            response = client.request("full", "https://priklad.invalid")
            self.assertIn(response.status, (503, 599))
            self.assertEqual(clock.sleeps, [10, 30])
            self.assertEqual(client.counts["full"]["dotazu"], 3)

    def test_http_200_errors_je_chyba(self):
        _, _, client = self.client([Response(200, {"errors": [{"status": 404, "message": "TAJNY_TITUL"}]})])
        self.assertEqual(client.request("recs", "https://priklad.invalid").status, 404)

    def test_deadline_nedovoli_dalsi_start(self):
        clock = Clock()
        limiter = Limiter(20, clock.now, clock.sleep)
        limiter.start(deadline=2)
        with self.assertRaises(StopSource):
            limiter.start(deadline=2)
        self.assertEqual(clock.now(), 0)

    def test_rozestup_i_pres_restart(self):
        with TemporaryDirectory() as folder:
            clock = Clock()
            first = Limiter(108, clock.now, clock.sleep, State(folder), clock.utcnow)
            first.start()
            second = Limiter(108, clock.now, clock.sleep, State(folder), clock.utcnow)
            second.start()
            self.assertGreaterEqual(clock.now(), 60 / 108 - 1e-6)

    def test_pet_noci_pauza_a_pryc(self):
        with TemporaryDirectory() as folder:
            state = State(folder)
            today = date(2026, 11, 3)
            for index in range(5):
                day = today + timedelta(days=index)
                state.failure("staff", 1101, day)
                state.failure("staff", 1101, day)
                self.assertEqual(state.get("staff", 1101)[1], index + 1)
            last = today + timedelta(days=4)
            self.assertFalse(state.eligible("staff", 1101, last + timedelta(days=13)))
            self.assertTrue(state.eligible("staff", 1101, last + timedelta(days=14)))
            state.success("staff", 1101, last + timedelta(days=14))
            self.assertEqual(state.get("staff", 1101)[1:], [0, 0, 0, 0])
            state.failure("stats", 1101, today, 404)
            self.assertTrue(state.gone(1101))
            state.failure("anilist", 1102, today, 404)
            self.assertFalse(state.gone(1102))


if __name__ == "__main__":
    unittest.main()

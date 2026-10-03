"""Закодированные позиции игроков (radar_crypt): подбор ключа на модели трафика.

Модель повторяет то, что видно в записи с сервера: позиция игрока — 8 байт (x, y float32)
с XOR-ключом, ключ общий для всех игроков и меняется каждые 10 с игрового времени
(границы сдвинуты на 2 с); скорость в событии движения открытая.
"""

import math
import random
import struct
import unittest

from albion_trader import radar_crypt as rc
from albion_trader.radar import Radar

T0 = 639266303000000000          # тики .NET, как в записи


def enc(x: float, y: float, key: bytes) -> bytes:
    raw = struct.pack("<ff", x, y)
    return bytes(a ^ b for a, b in zip(raw, key))


def move_block(ticks: int, pos: bytes, speed: float, target: bytes) -> bytes:
    return bytes([3]) + struct.pack("<q", ticks) + pos + bytes([0x64]) + struct.pack("<f", speed) + target


class Model:
    def __init__(self, seed=1, players=8, center=(110.0, 115.0)):
        self.rng = random.Random(seed)
        self.center = center
        self.keys = {}
        self.players = {}
        for i in range(players):
            ang = self.rng.uniform(0, 2 * math.pi)
            r = self.rng.uniform(5, 60)
            self.players[1000 + i] = [center[0] + r * math.cos(ang), center[1] + r * math.sin(ang),
                                      self.rng.uniform(0, 2 * math.pi), 5.5]

    def key(self, ticks):
        w = rc.window_of(ticks)
        if w not in self.keys:
            self.keys[w] = bytes(self.rng.randrange(256) for _ in range(8))
        return self.keys[w]

    def step(self, dt):
        for p in self.players.values():
            if self.rng.random() < 0.05:
                p[2] += self.rng.uniform(-1.5, 1.5)
            p[0] += math.cos(p[2]) * p[3] * dt
            p[1] += math.sin(p[2]) * p[3] * dt
            # не уходить далеко от центра
            if math.hypot(p[0] - self.center[0], p[1] - self.center[1]) > 65:
                p[2] += math.pi


class DecoderTest(unittest.TestCase):
    def run_model(self, seed, seconds=60):
        m = Model(seed)
        r = Radar()
        r.on_join({0: 1, 2: "Me", 9: list(m.center)})
        r.me["x"], r.me["y"] = m.center
        ticks = T0 + 30_000_000
        for oid, (x, y, _a, _s) in m.players.items():
            r.decoder.observe(ticks)
            r.on_event("new_character", {0: oid, 1: f"P{oid}", 12: 0, 13: "", 16: enc(x, y, m.key(ticks))})
        errors, decoded, total = [], 0, 0
        steps = int(seconds / 0.1)
        for i in range(steps):
            ticks += 1_000_000                       # 0,1 с
            m.step(0.1)
            for oid, (x, y, _a, speed) in m.players.items():
                k = m.key(ticks)
                r.on_event("move", {0: oid, 1: move_block(ticks, enc(x, y, k), speed, enc(x, y, k))})
                if i > 50:                           # после первых 5 секунд
                    total += 1
                    ent = r.entities.get(oid)
                    if ent is not None:
                        decoded += 1
                        errors.append(math.hypot(ent.x - x, ent.y - y))
        return errors, decoded, total, r

    def test_positions_recovered(self):
        for seed in (1, 2, 3):
            errors, decoded, total, r = self.run_model(seed, seconds=40)
            errors.sort()
            self.assertGreater(decoded / total, 0.9, seed)
            self.assertLess(errors[len(errors) // 2], 0.5, seed)           # медиана — доли метра
            self.assertLess(errors[int(len(errors) * 0.9)], 3.0, seed)     # 90 % — до 3 м
            snap = r.snapshot(touch=False)
            self.assertEqual(sum(e["kind"] == "player" for e in snap["entities"]), 8)

    def run_crowd(self, seed, standing=15, movers=3, seconds=40):
        """Город: толпа стоит вокруг (3–15 м), несколько ходят; стоящие появляются по ходу."""
        rng = random.Random(seed)
        m = Model(seed, players=movers)
        r = Radar()
        r.on_join({0: 1, 2: "Me", 9: list(m.center)})
        r.me["x"], r.me["y"] = m.center
        crowd = {}
        for i in range(standing):
            ang, dist = rng.uniform(0, 2 * math.pi), rng.uniform(3, 15)
            crowd[2000 + i] = (m.center[0] + dist * math.cos(ang), m.center[1] + dist * math.sin(ang),
                               rng.randrange(0, int(seconds * 10)))
        ticks = T0 + 30_000_000
        r.decoder.observe(ticks)
        for oid, (x, y, _a, _s) in m.players.items():
            r.on_event("new_character", {0: oid, 1: f"M{oid}", 12: 0, 13: "", 16: enc(x, y, m.key(ticks))})
        for i in range(int(seconds * 10)):
            ticks += 1_000_000
            m.step(0.1)
            r.decoder.observe(ticks)
            for oid, (x, y, at) in crowd.items():
                if at == i:                                  # появился и стоит
                    p = enc(x, y, m.key(ticks))
                    r.on_event("new_character", {0: oid, 1: f"S{oid}", 12: 0, 13: "", 16: p, 17: p})
            for oid, (x, y, _a, speed) in m.players.items():
                k = m.key(ticks)
                r.on_event("move", {0: oid, 1: move_block(ticks, enc(x, y, k), speed, enc(x, y, k))})
        ticks += 200_000_000                                 # прошло ещё 20 с
        for _ in range(30):
            ticks += 1_000_000
            m.step(0.1)
            for oid, (x, y, _a, speed) in m.players.items():
                k = m.key(ticks)
                r.on_event("move", {0: oid, 1: move_block(ticks, enc(x, y, k), speed, enc(x, y, k))})
        snap = r.snapshot(touch=False)
        found = {e["id"]: e for e in snap["entities"] if e["kind"] == "player"}
        errs = [math.hypot(found[oid]["x"] - x, found[oid]["y"] - y) for oid, (x, y, _a) in crowd.items()
                if oid in found]
        return len(errs), sorted(errs)

    def test_standing_crowd(self):
        for seed in (1, 2, 3):
            n, errs = self.run_crowd(seed)
            self.assertGreaterEqual(n, 14, seed)                       # стоящие не теряются
            self.assertLess(errs[len(errs) // 2], 2.0, seed)

    def test_move_parts_and_edges(self):
        self.assertIsNone(rc.move_parts(b"\x03" * 10))
        self.assertIsNone(rc.move_parts([1.0, 2.0]))
        ticks, pos, speed = rc.move_parts(move_block(T0, b"\x01" * 8, 5.5, b"\x02" * 8))
        self.assertEqual((ticks, pos, speed), (T0, b"\x01" * 8, 5.5))
        self.assertEqual(rc.move_parts(move_block(T0, b"\x01" * 8, float("nan"), b"\x02" * 8))[2], 0.0)
        d = rc.PositionDecoder()
        self.assertIsNone(d.decode(1, b"\x00" * 8))                       # времени ещё нет
        d.add(1, T0, b"\x00" * 8)
        self.assertIsNone(d.decode(1, b"\x00" * 8, T0))                   # мало образцов — ключа нет

    def test_pending_players_listed_and_threat(self):
        from albion_trader.bot_threat import DANGER, ThreatTracker
        r = Radar()
        r.on_join({0: 1, 2: "Me", 9: [100.0, 100.0]})
        r.decoder.observe(T0 + 30_000_000)
        r.on_event("new_character", {0: 7, 1: "Gank", 12: 0, 13: "", 53: 255, 16: b"\x01" * 8})
        r.on_event("new_character", {0: 8, 1: "Calm", 12: 0, 13: "", 16: b"\x02" * 8})
        snap = r.snapshot(touch=False)
        self.assertEqual(sorted(p["name"] for p in snap["pending_players"]), ["Calm", "Gank"])
        self.assertTrue(snap["encrypted"])
        self.assertIsNone(snap["pending_players"][0]["dist"])

        class Feed:
            radar = r

            @staticmethod
            def entities(kind=None):
                return []
        threats = ThreatTracker().assess(Feed, (100.0, 100.0), {"player_radius": 45})
        self.assertEqual([(t.name, t.level) for t in threats], [("Gank", DANGER)])   # мирный без позиции — нет
        self.assertIn("позиция уточняется", threats[0].text())
        self.assertEqual((threats[0].entity.x, threats[0].entity.faction), (100.0, 255))

    def test_plain_positions_untouched(self):
        r = Radar()
        r.on_join({0: 1, 2: "Me", 9: [10.0, 10.0]})
        r.on_event("new_character", {0: 5, 1: "Bob", 12: [13.0, 14.0]})
        self.assertEqual((r.entities[5].x, r.entities[5].y), (13.0, 14.0))
        self.assertNotIn(5, r.enc_ids)
        r.on_event("leave", {0: 5})
        self.assertEqual(r.entities, {})
        for i in (6, 7, 8):
            r.on_event("new_character", {0: i, 1: f"P{i}", 12: [1.0, float(i)]})
        r.on_event("leave", {0: [6, 7]})                 # уход пачкой — список id
        self.assertEqual(list(r.entities), [8])


if __name__ == "__main__":
    unittest.main()

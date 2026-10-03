import json
import struct
import tempfile
import time
import unittest
from pathlib import Path

from albion_trader.capture import photon
from albion_trader.capture.albion import AlbionState, normalize_location_id, normalize_zone
from albion_trader.capture.sniffer import Sniffer, parse_ipv4_udp, read_pcap
from albion_trader.server import App, AppConfig

try:
    from . import photon_builder as pb
except ImportError:  # запуск через discover -s tests
    import photon_builder as pb

OFFERS, REQUESTS, JOIN, CLUSTER, HISTORY = 81, 82, 2, 17, 95


def order(oid, price, typ="offer", loc=None, quality=1):
    o = {"Id": oid, "ItemTypeId": "T4_BAG", "ItemGroupTypeId": "T4_BAG", "LocationId": loc,
         "QualityLevel": quality, "EnchantmentLevel": 0, "UnitPriceSilver": price * 10000,
         "Amount": 3, "AuctionType": typ, "Expires": "2099-01-01T00:00:00"}
    return o


class Collector:
    def __init__(self):
        self.items = []

    def __call__(self, topic, payload):
        self.items.append((topic, payload))
        return len(payload.get("Orders", payload.get("MarketHistories", [])))


def make_state():
    sink = Collector()
    state = AlbionState(sink)
    parser = photon.PhotonParser(state.on_request, state.on_response, state.on_event, state.on_encrypted)
    return sink, state, parser


class PhotonParserTest(unittest.TestCase):
    def test_request_params(self):
        got = {}
        p = photon.PhotonParser(on_request=lambda code, params: got.update(code=code, params=params))
        self.assertTrue(p.receive_packet(pb.packet(pb.request(OFFERS, {1: "x", 2: -5, 3: 300000}))))
        self.assertEqual(got["params"][1], "x")
        self.assertEqual(got["params"][2], -5)
        self.assertEqual(got["params"][3], 300000)
        self.assertEqual(got["params"][253], OFFERS)

    def test_response_return_code_and_orders_array(self):
        got = []
        p = photon.PhotonParser(on_response=lambda *a: got.append(a))
        p.receive_packet(pb.packet(pb.response(JOIN, {8: "3005"}, return_code=5)))
        self.assertEqual(got[0][1], 5)
        self.assertEqual(got[0][3][8], "3005")
        p.receive_packet(pb.packet(pb.orders_response([order(1, 10)])))
        self.assertIsInstance(got[1][3][0], list)

    def test_multiple_commands_and_coalesced_packets(self):
        seen = []
        p = photon.PhotonParser(on_request=lambda c, prm: seen.append(prm[1]))
        pkt1 = pb.packet(pb.request(1, {1: "a"}), pb.request(1, {1: "b"}))
        pkt2 = pb.packet(pb.request(1, {1: "c"}))
        self.assertTrue(p.receive_packet(pkt1 + pkt2))
        self.assertEqual(seen, ["a", "b", "c"])

    def test_fragment_reassembly(self):
        seen = []
        p = photon.PhotonParser(on_request=lambda c, prm: seen.append(prm[1]))
        data = pb.command(2, bytes([1]) + pb.params({1: "x" * 300}))[12:]  # тело reliable-команды
        half = len(data) // 2
        for num, (off, chunk) in enumerate(((0, data[:half]), (half, data[half:]))):
            frag = struct.pack(">IIIII", 5, 2, num, len(data), off) + chunk
            cmd = bytes([8, 0, 0, 0]) + struct.pack(">II", 12 + len(frag), num) + frag
            p.receive_packet(pb.packet(cmd))
        self.assertEqual(seen, ["x" * 300])
        self.assertEqual(p.pending, {})

    def test_pending_segments_bounded(self):
        p = photon.PhotonParser()
        for i in range(photon.MAX_PENDING_SEGMENTS * 3):
            frag = struct.pack(">IIIII", i, 2, 0, 20, 0) + b"\0" * 10
            p.receive_packet(pb.packet(bytes([8, 0, 0, 0]) + struct.pack(">II", 12 + len(frag), 0) + frag))
        self.assertLessEqual(len(p.pending), photon.MAX_PENDING_SEGMENTS)

    def test_encrypted_packet(self):
        hits = []
        p = photon.PhotonParser(on_encrypted=lambda: hits.append(1))
        self.assertFalse(p.receive_packet(pb.packet(pb.request(1, {}), flags=1)))
        self.assertEqual(hits, [1])

    def test_crc_packet(self):
        seen = []
        p = photon.PhotonParser(on_request=lambda c, prm: seen.append(prm[1]))
        cmd = pb.request(1, {1: "ok"})
        header = struct.pack(">hBBii", 7, 0xCC, 1, 0, 99)
        body = header + b"\0\0\0\0" + cmd
        crc = photon.photon_crc(body)
        self.assertTrue(p.receive_packet(header + struct.pack(">I", crc) + cmd))
        bad = header + struct.pack(">I", crc ^ 1) + cmd
        self.assertFalse(p.receive_packet(bad))
        self.assertEqual(seen, ["ok"])

    def test_garbage_does_not_crash(self):
        import random
        rnd = random.Random(1)
        p = photon.PhotonParser(lambda *a: None, lambda *a: None, lambda *a: None)
        base = pb.packet(pb.request(1, {1: "abc", 2: [1, 2, 3]}))
        for _ in range(2000):
            b = bytearray(base)
            for _ in range(rnd.randint(1, 6)):
                b[rnd.randrange(len(b))] = rnd.randrange(256)
            p.receive_packet(bytes(b))
        for _ in range(500):
            p.receive_packet(bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 200))))


class AlbionStateTest(unittest.TestCase):
    def test_orders_attributed_to_current_location(self):
        sink, state, parser = make_state()
        parser.receive_packet(pb.packet(pb.response(JOIN, {8: "3003"})))
        parser.receive_packet(pb.packet(pb.orders_response([order(1, 500, "request")])))
        topic, payload = sink.items[0]
        self.assertEqual(topic, "marketorders.ingest")
        self.assertEqual(payload["Orders"][0]["LocationId"], "3003")

    def test_orders_keep_own_location(self):
        sink, state, parser = make_state()
        parser.receive_packet(pb.packet(pb.request(CLUSTER, {0: "0007"})))
        parser.receive_packet(pb.packet(pb.orders_response([order(1, 10, loc="BLACKBANK-2310")])))
        self.assertEqual(sink.items[0][1]["Orders"][0]["LocationId"], "BLACKBANK-2310")
        self.assertEqual(state.location, "0007")

    def test_orders_without_location_dropped(self):
        sink, state, parser = make_state()
        parser.receive_packet(pb.packet(pb.orders_response([order(1, 10)])))
        self.assertEqual(sink.items, [])
        self.assertEqual(state.stats["no_location_drops"], 1)

    def test_orders_via_param_table(self):
        sink, state, parser = make_state()
        state.location = "4002"
        import json
        parser.receive_packet(pb.packet(pb.response(REQUESTS, {0: [json.dumps(order(1, 7, "request"))]})))
        self.assertEqual(sink.items[0][1]["Orders"][0]["LocationId"], "4002")

    def test_history_request_response(self):
        sink, state, parser = make_state()
        state.location = "3005"
        parser.receive_packet(pb.packet(pb.request(HISTORY, {1: -121, 2: 2, 3: 1, 255: 77})))
        parser.receive_packet(pb.packet(pb.response(
            HISTORY, {0: [5, -3, -200], 1: [1000, 2000, 3000], 2: [10, 30, 20], 255: 77})))
        topic, payload = sink.items[0]
        self.assertEqual(topic, "markethistories.ingest")
        self.assertEqual(payload["AlbionId"], 135)
        self.assertEqual((payload["QualityLevel"], payload["Timescale"]), (2, 1))
        self.assertEqual([h["ItemAmount"] for h in payload["MarketHistories"]], [253, 5])

    def test_swapped_opcode(self):
        sink, state, parser = make_state()
        parser.receive_packet(pb.packet(pb.response(JOIN << 8, {8: "2004"})))
        self.assertEqual(state.location, "2004")

    def test_encryption_detected_after_market_request(self):
        now = [1000.0]
        sink = Collector()
        state = AlbionState(sink, clock=lambda: now[0])
        parser = photon.PhotonParser(state.on_request, state.on_response, None, state.on_encrypted)
        parser.receive_packet(pb.packet(pb.request(1, {1: "x"}), flags=1))
        self.assertIsNone(state.stats["encrypted_at"])
        parser.receive_packet(pb.packet(pb.request(OFFERS, {})))
        now[0] += 1
        parser.receive_packet(pb.packet(pb.request(1, {1: "x"}), flags=1))
        self.assertEqual(state.stats["encrypted_at"], 1001.0)

    def test_normalize_location_id(self):
        self.assertEqual(normalize_location_id("3013-Auction2"), "3013-Auction2")
        self.assertEqual(normalize_location_id("0007"), "0007")
        self.assertEqual(normalize_location_id("hello"), "")
        self.assertEqual(normalize_location_id(5), "")


class SnifferTest(unittest.TestCase):
    def test_parse_ipv4_udp(self):
        self.assertEqual(parse_ipv4_udp(pb.ip_udp(b"hello")), b"hello")
        self.assertIsNone(parse_ipv4_udp(pb.ip_udp(b"hello", src_port=1, dst_port=2)))
        self.assertIsNone(parse_ipv4_udp(b"\x60" + b"\0" * 40))

    def test_end_to_end_into_database(self):
        with tempfile.TemporaryDirectory() as d:
            app = App(AppConfig(db_path=Path(d) / "m.db", items_path=Path(d) / "i.json", capture=False))
            packets = [
                pb.ip_udp(pb.packet(pb.response(JOIN, {8: "0007"}))),
                pb.ip_udp(pb.packet(pb.orders_response([order(1, 1000), order(2, 1200)]))),
                pb.ip_udp(pb.packet(pb.response(JOIN, {8: "3003"}))),
                pb.ip_udp(pb.packet(pb.orders_response([order(3, 5000, "request")]))),
                pb.ip_udp(b"not photon at all"),
            ]
            fake = FakeSocket(packets)
            app.start_capture(open_sockets=lambda: [fake])
            deadline = time.time() + 5
            while fake.packets and time.time() < deadline:
                time.sleep(0.01)
            app.sniffer.stop()
            status = app.api_status({})
            self.assertEqual(status["total_orders"], 3)
            self.assertEqual(status["capture"]["location"], "3003")
            self.assertEqual(status["capture"]["location_name"], "Карлеон, город (Чёрный рынок)")
            deals = app.api_deals({"max_age": "1"})
            self.assertEqual(deals["count"], 1)
            self.assertEqual(deals["deals"][0]["destination"], "black_market")

    def test_follows_game_local_ports_on_other_server_port(self):
        from albion_trader.capture.sniffer import _is_albion_udp
        state = AlbionState(Collector())
        s = Sniffer(state)
        got = []
        s.taps.append(lambda port, payload: got.append(port))
        pkt = pb.ip_udp(pb.packet(pb.request(21, {1: [1.0, 2.0]})), src_port=50007, dst_port=6123)
        s.feed_ip_packet(pkt)
        self.assertEqual(got, [])                         # сервер не на 5056, порт игры неизвестен
        self.assertFalse(_is_albion_udp(pkt, (5056,)))
        s.set_local_ports({50007})
        self.assertTrue(_is_albion_udp(pkt, (5056,), s.local_ports))
        s.feed_ip_packet(pkt)
        s.feed_ip_packet(pb.ip_udp(pb.packet(pb.event(3, {0: 1})), src_port=6123, dst_port=50007))
        self.assertEqual(got, [50007, 50007])              # исходящий и входящий — порт игры
        self.assertEqual(state.stats["requests"], 1)

    def test_fragments_of_two_connections_do_not_mix(self):
        # Два подключения игры (прошлая и новая зона) шлют большие события кусками
        # с одинаковым номером начала — каждое должно собраться целым.
        state = AlbionState(Collector())
        names = []
        state.on("event:new_character", lambda prm: names.append(prm[1]))
        s = Sniffer(state)
        s.set_local_ports({50007, 50008})

        def frags(name):
            data = pb.command(4, bytes([state.ev["new_character"]]) + pb.params({0: 1, 1: name * 200}))[12:]
            half = len(data) // 2
            out = []
            for num, (off, chunk) in enumerate(((0, data[:half]), (half, data[half:]))):
                frag = struct.pack(">IIIII", 7, 2, num, len(data), off) + chunk
                out.append(pb.packet(bytes([8, 0, 0, 0]) + struct.pack(">II", 12 + len(frag), num) + frag))
            return out
        a, b = frags("A"), frags("B")
        for payload, port in ((a[0], 50007), (b[0], 50008), (a[1], 50007), (b[1], 50008)):
            s.feed_ip_packet(pb.ip_udp(payload, src_port=5056, dst_port=port))
        self.assertEqual(names, ["A" * 200, "B" * 200])
        self.assertEqual(s.evicted_segments, 0)

    def test_capture_error_reported(self):
        from albion_trader.capture.sniffer import CaptureError

        def fail():
            raise CaptureError("нужны права администратора")
        s = Sniffer(AlbionState(Collector()), open_sockets=fail)
        self.assertFalse(s.start())
        self.assertIn("администратора", s.status["error"])

    def test_read_pcap(self):
        frames = [b"\0" * 12 + b"\x08\x00" + pb.ip_udp(pb.packet(pb.response(JOIN, {8: "5003"})))]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.pcap"
            with open(path, "wb") as f:
                f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
                for fr in frames:
                    f.write(struct.pack("<IIII", 0, 0, len(fr), len(fr)) + fr)
            sink, state, _ = make_state()
            s = Sniffer(state)
            for ip in read_pcap(str(path)):
                s.feed_ip_packet(ip)
            self.assertEqual(state.location, "5003")


class FakeSocket:
    def __init__(self, packets):
        self.packets = list(packets)

    def recvfrom(self, _n):
        import socket
        if self.packets:
            return self.packets.pop(0), None
        raise socket.timeout()

    def close(self):
        pass


if __name__ == "__main__":
    unittest.main()


class LostResponseTest(unittest.TestCase):
    def test_lost_market_response_counted(self):
        now = [1000.0]
        sink = Collector()
        state = AlbionState(sink, clock=lambda: now[0])
        parser = photon.PhotonParser(state.on_request, state.on_response, None, state.on_encrypted)
        state.location = "3005"
        # Игра шлёт пачку из 4 запросов и получает 4 ответа — это не потери.
        for code in (REQUESTS, OFFERS, OFFERS, REQUESTS):
            parser.receive_packet(pb.packet(pb.request(code, {})))
        for typ in ("request", "offer", "offer", "request"):
            parser.receive_packet(pb.packet(pb.orders_response([order(1, 10, typ)])))
        # Один запрос остался без ответа.
        parser.receive_packet(pb.packet(pb.request(OFFERS, {})))
        now[0] += 60
        parser.receive_packet(pb.packet(pb.request(OFFERS, {})))
        parser.receive_packet(pb.packet(pb.response(OFFERS, {})))  # пустой ответ
        self.assertEqual(state.stats["market_requests"], 6)
        self.assertEqual(state.stats["market_responses_lost"], 1)
        self.assertEqual(state.pending_market_requests, [])

    def test_record_and_replay_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "rec.pcap")
            packets = [pb.ip_udp(pb.packet(pb.response(JOIN, {8: "1301"}))),
                       pb.ip_udp(pb.packet(pb.orders_response([order(1, 10)])))]
            fake = FakeSocket(packets)
            s = Sniffer(AlbionState(Collector()), open_sockets=lambda: [fake], record_path=path)
            s.start()
            deadline = time.time() + 5
            while (fake.packets or s.status["packets"] < 2) and time.time() < deadline:
                time.sleep(0.01)
            s.stop()
            sink, state, _ = make_state()
            replay = Sniffer(state)
            for ip in read_pcap(path):
                replay.feed_ip_packet(ip)
            self.assertEqual(state.location, "1301")
            self.assertEqual(len(sink.items), 1)


class DuplicateTest(unittest.TestCase):
    def test_same_packet_from_two_adapters_processed_once(self):
        sink, state, _ = make_state()
        s = Sniffer(state)
        payload = pb.packet(pb.response(JOIN, {8: "3005"}))
        state.location = ""
        first = pb.ip_udp(payload)
        # Тот же пакет через виртуальный адаптер: другой адрес получателя и TTL.
        second = bytearray(first)
        second[8] = 128
        second[16:20] = bytes([172, 19, 0, 1])
        s.feed_ip_packet(first)
        s.feed_ip_packet(bytes(second))
        self.assertEqual(s.status["packets"], 1)
        self.assertEqual(s.status["duplicates"], 1)

    def test_duplicate_fragments_do_not_complete_message_early(self):
        seen = []
        p = photon.PhotonParser(on_request=lambda c, prm: seen.append(prm[1]))
        data = pb.command(2, bytes([1]) + pb.params({1: "y" * 3000}))[12:]
        chunk = 1000
        n = (len(data) + chunk - 1) // chunk

        def frag(k):
            part = data[k * chunk:(k + 1) * chunk]
            f = struct.pack(">IIIII", 9, n, k, len(data), k * chunk) + part
            return pb.packet(bytes([8, 0, 0, 0]) + struct.pack(">II", 12 + len(f), k) + f)
        # Первый кусок приходит трижды, затем остальные.
        for k in [0, 0, 0] + list(range(1, n)):
            p.receive_packet(frag(k))
        self.assertEqual(seen, ["y" * 3000])


class DispatcherTest(unittest.TestCase):
    def test_character_name_from_join(self):
        sink, state, parser = make_state()
        parser.receive_packet(pb.packet(pb.response(JOIN, {2: "Hero", 8: "3005"})))
        self.assertEqual(state.character_name, "Hero")
        self.assertEqual(state.stats["character"], "Hero")

    def test_zone_and_object_id_from_join(self):
        sink, state, parser = make_state()
        zones = []
        state.on("zone", lambda z, prev: zones.append((z, prev)))
        parser.receive_packet(pb.packet(pb.response(JOIN, {0: 4242, 2: "Hero", 8: "3004"})))
        self.assertEqual((state.object_id, state.zone, state.location), (4242, "3004", "3004"))
        # Дорога Авалона: рыночная локация не меняется, зона — меняется.
        parser.receive_packet(pb.packet(pb.response(JOIN, {0: 4243, 2: "Hero", 8: "TNL-001"})))
        self.assertEqual((state.object_id, state.zone, state.location), (4243, "TNL-001", "3004"))
        parser.receive_packet(pb.packet(pb.request(CLUSTER, {0: "0f1e-uuid@4206"})))
        parser.receive_packet(pb.packet(pb.request(CLUSTER, {0: "0f1e-uuid@4206"})))   # повтор — без события
        self.assertEqual(zones, [("3004", ""), ("TNL-001", "3004"), ("4206", "TNL-001")])
        self.assertEqual(state.stats["zone"], "4206")

    def test_normalize_zone(self):
        self.assertEqual(normalize_zone("DNG-KPR-02-MAIN-04"), "DNG-KPR-02-MAIN-04")
        self.assertEqual(normalize_zone("abc@TNL-017"), "TNL-017")
        self.assertEqual(normalize_zone("@island@0e8a6c1b-2d6a-4bd4-9f8f-6f1f1f1f1f1f"),
                         "@ISLAND@0e8a6c1b-2d6a-4bd4-9f8f-6f1f1f1f1f1f")
        for bad in (None, 5, "", "  ", "a b", "x" * 200, "<script>"):
            self.assertEqual(normalize_zone(bad), "", bad)

    def test_new_events_dispatched(self):
        sink, state, parser = make_state()
        got = []
        for name in ("harvest_finished", "craft_item_finished", "fishing_finished", "new_loot_chest",
                     "loot_chest_opened"):
            state.on("event:" + name, lambda p, n=name: got.append((n, p.get(0))))
        for code in (61, 71, 358, 393, 395):
            parser.receive_packet(pb.packet(pb.event(code, {0: code})))
        self.assertEqual(got, [("harvest_finished", 61), ("craft_item_finished", 71), ("fishing_finished", 358),
                               ("new_loot_chest", 393), ("loot_chest_opened", 395)])

    def test_my_orders_not_stored_as_market(self):
        sink, state, parser = make_state()
        got = []
        state.on("my_orders", lambda kind, orders: got.append((kind, len(orders))))
        state.location = "3005"
        mine = order(1, 10)
        # Ответ «мои предложения» (код 92) — строковый массив на месте debug-сообщения.
        body = bytes([92]) + struct.pack("<h", 0) + pb.value([json.dumps(mine)]) + pb.params({})
        parser.receive_packet(pb.packet(pb.command(3, body)))
        self.assertEqual(sink.items, [])
        self.assertEqual(got, [("offers", 1)])

    def test_unknown_code_all_mine_classified_by_content(self):
        sink, state, parser = make_state()
        got = []
        state.on("my_orders", lambda kind, orders: got.append(kind))
        state.location = "3005"
        state.character_name = "Hero"
        mine = dict(order(1, 10), SellerName="Hero")
        body = bytes([200]) + struct.pack("<h", 0) + pb.value([json.dumps(mine)]) + pb.params({})
        parser.receive_packet(pb.packet(pb.command(3, body)))
        self.assertEqual((sink.items, got), ([], ["mine"]))
        # Обычный просмотр рынка, где встречается и ваш заказ, остаётся рынком.
        other = dict(order(2, 12), SellerName="Someone")
        parser.receive_packet(pb.packet(pb.orders_response([mine, other])))
        self.assertEqual(len(sink.items), 1)

    def test_events_dispatched_by_name(self):
        sink, state, parser = make_state()
        got = []
        state.on("event:update_fame", lambda p: got.append(p[2]))
        ev = pb.command(4, bytes([1]) + pb.params({2: 1234, 252: 82}))
        parser.receive_packet(pb.packet(ev))
        self.assertEqual(got, [1234])
        self.assertEqual(state.stats["events"], 1)

    def test_move_events_filtered_before_parsing(self):
        seen = []
        p = photon.PhotonParser(on_event=lambda c, prm: seen.append(c), event_filter=AlbionState.wants_event)
        p.receive_packet(pb.packet(pb.command(4, bytes([3]) + pb.params({1: 5}))))
        p.receive_packet(pb.packet(pb.command(4, bytes([1]) + pb.params({252: 82}))))
        self.assertEqual(seen, [1])

    def test_request_and_response_listeners(self):
        sink, state, parser = make_state()
        got = []
        state.on("request:gold_market_get_average_info", lambda p: got.append("req"))
        state.on("response:gold_market_get_average_info", lambda p: got.append(p[0]))
        parser.receive_packet(pb.packet(pb.request(250, {})))
        parser.receive_packet(pb.packet(pb.response(250, {0: [5000, 5100]})))
        self.assertEqual(got, ["req", [5000, 5100]])

    def test_opcodes_override_file(self):
        from albion_trader.capture.albion import load_opcodes
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "opcodes.json"
            path.write_text(json.dumps({"auction_get_offers": 90, "events": {"update_fame": 83}}))
            codes = load_opcodes(path)
        self.assertEqual(codes["auction_get_offers"], 90)
        self.assertEqual(codes["events"]["update_fame"], 83)
        state = AlbionState(Collector(), codes)
        self.assertEqual(state.ev["update_fame"], 83)
        self.assertEqual(state.op["auction_get_offers"], 90)


class GoldCaptureTest(unittest.TestCase):
    def test_gold_response_stored(self):
        from albion_trader.server import App, AppConfig
        with tempfile.TemporaryDirectory() as d:
            app = App(AppConfig(db_path=Path(d) / "m.db", items_path=Path(d) / "i.json", capture=False))
            parser = photon.PhotonParser(app.albion.on_request, app.albion.on_response, app.albion.on_event)
            ticks = lambda ts: ts * 10_000_000 + 621_355_968_000_000_000
            now = int(time.time())
            parser.receive_packet(pb.packet(pb.response(250, {0: [4800, 4900], 1: [ticks(now - 3600), ticks(now)]})))
            data = app.api_gold({"days": "1"})
            self.assertEqual([p["price"] for p in data["prices"]], [4800, 4900])
            self.assertEqual(data["current"]["price"], 4900)


class PatchResilienceTest(unittest.TestCase):
    def test_orders_with_unknown_code_saved_by_content(self):
        sink, state, parser = make_state()
        state.location = "3008"
        # Код 81 сдвинулся на 84, и ответ пришёл таблицей параметров, а не в слоте debug.
        parser.receive_packet(pb.packet(pb.response(84, {0: [json.dumps(order(1, 10))], 1: 5})))
        self.assertEqual(len(sink.items), 1)
        self.assertEqual(state.stats["orders_by_content"], 1)

    def test_set_opcodes_live(self):
        sink, state, parser = make_state()
        state.set_opcodes({"join": 7, "events": {"update_fame": 90}})
        parser.receive_packet(pb.packet(pb.response(7, {8: "5003"})))
        self.assertEqual(state.location, "5003")
        self.assertEqual(state.ev["update_fame"], 90)
        self.assertEqual(state.op["auction_get_offers"], 81)   # остальное — по умолчанию

    def test_parse_go_enum(self):
        from albion_trader.capture.opcodes import build_opcodes, parse_go_enum
        ops = "const (\n\topUnused OperationType = iota\n\topPing\n\topJoin // вход\n\topFoo = 10\n\topBar\n)\n"
        self.assertEqual(parse_go_enum(ops, "op"), {"opUnused": 0, "opPing": 1, "opJoin": 2, "opFoo": 10, "opBar": 11})
        with self.assertRaises(ValueError):
            build_opcodes(ops, "")

import struct
import tempfile
import time
import unittest
from pathlib import Path

from albion_trader.capture import photon
from albion_trader.capture.albion import AlbionState, normalize_location_id
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
            self.assertEqual(status["capture"]["location_name"], "Чёрный рынок")
            deals = app.api_deals({"max_age": "1"})
            self.assertEqual(deals["count"], 1)
            self.assertEqual(deals["deals"][0]["destination"], "black_market")

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
        sink, state, parser = make_state()
        state.location = "3005"
        parser.receive_packet(pb.packet(pb.request(OFFERS, {})))
        parser.receive_packet(pb.packet(pb.orders_response([order(1, 10)])))
        parser.receive_packet(pb.packet(pb.request(OFFERS, {})))
        # ответ потерян
        parser.receive_packet(pb.packet(pb.request(OFFERS, {})))
        parser.receive_packet(pb.packet(pb.response(OFFERS, {})))  # пустой ответ — не потеря
        parser.receive_packet(pb.packet(pb.request(OFFERS, {})))
        self.assertEqual(state.stats["market_requests"], 4)
        self.assertEqual(state.stats["market_responses_lost"], 1)

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

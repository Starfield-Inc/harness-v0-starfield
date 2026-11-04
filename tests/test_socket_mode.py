import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from harness.socket_mode import AuthError, Closed, WebSocket, accept_key, decode_frame, encode_frame, open_url, serve
from tests.fakes.slack_server import FakeSlack, read_client_frame, server_frame

TOKEN = "xapp-CANARY-1"
RFC_MASK = bytes([0x37, 0xFA, 0x21, 0x3D])
HELLO = bytes([0x48, 0x65, 0x6C, 0x6C, 0x6F])
MASKED_HELLO = bytes([0x37, 0xFA, 0x21, 0x3D, 0x7F, 0x9F, 0x4D, 0x51, 0x58])


class Rfc6455Vectors(unittest.TestCase):
    def test_accept_key_from_section_1_3(self):
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_unmasked_hello(self):
        frame = bytes([0x81, 0x05]) + HELLO
        self.assertEqual(encode_frame(0x1, b"Hello", None), frame)
        self.assertEqual(decode_frame(frame), (0x1, True, b"Hello", 7))

    def test_masked_hello_is_what_the_client_sends(self):
        self.assertEqual(encode_frame(0x1, b"Hello", RFC_MASK), bytes([0x81, 0x85]) + MASKED_HELLO)

    def test_fragmented_hel_lo(self):
        first, second = bytes([0x01, 0x03, 0x48, 0x65, 0x6C]), bytes([0x80, 0x02, 0x6C, 0x6F])
        self.assertEqual(decode_frame(first + second), (0x1, False, b"Hel", 5))
        self.assertEqual(decode_frame(second), (0x0, True, b"lo", 4))

    def test_ping_and_masked_pong(self):
        self.assertEqual(decode_frame(bytes([0x89, 0x05]) + HELLO), (0x9, True, b"Hello", 7))
        self.assertEqual(encode_frame(0xA, b"Hello", RFC_MASK), bytes([0x8A, 0x85]) + MASKED_HELLO)

    def test_16_bit_length_from_126_bytes(self):
        frame = encode_frame(0x2, b"x" * 126, None)
        self.assertEqual(frame[:4], bytes([0x82, 0x7E, 0x00, 0x7E]))
        self.assertEqual(decode_frame(frame), (0x2, True, b"x" * 126, 130))
        self.assertEqual(encode_frame(0x2, b"x" * 125, None)[:2], bytes([0x82, 0x7D]))
        self.assertEqual(encode_frame(0x2, b"x" * 65535, None)[:4], bytes([0x82, 0x7E, 0xFF, 0xFF]))

    def test_64_bit_length_from_65536_bytes(self):
        frame = encode_frame(0x2, b"y" * 65536, None)
        self.assertEqual(frame[:10], bytes([0x82, 0x7F, 0, 0, 0, 0, 0, 0x01, 0x00, 0x00]))
        self.assertEqual(decode_frame(frame), (0x2, True, b"y" * 65536, 65546))

    def test_masked_64_bit_frame_round_trips_through_the_fake(self):
        frame = encode_frame(0x1, b"z" * 70000, RFC_MASK)
        with tempfile.TemporaryFile() as f:
            f.write(frame)
            f.seek(0)
            self.assertEqual(read_client_frame(f), (0x1, True, True, b"z" * 70000))

    def test_incomplete_buffer_is_none(self):
        frame = encode_frame(0x1, b"x" * 300, None)
        for cut in (0, 1, 3, 299):
            self.assertIsNone(decode_frame(frame[:cut]))


class Refusals(unittest.TestCase):
    def refuse(self, data, words):
        with self.assertRaises(Closed) as caught:
            decode_frame(data)
        self.assertIn(words, str(caught.exception))

    def test_masked_server_frame(self):
        self.refuse(bytes([0x81, 0x85]) + MASKED_HELLO, "masked")

    def test_rsv_bits(self):
        for rsv in (0x40, 0x20, 0x10):
            self.refuse(bytes([0x81 | rsv, 0x05]) + HELLO, "RSV")

    def test_unknown_opcodes(self):
        for opcode in (0x3, 0x7, 0xB, 0xF):
            self.refuse(bytes([0x80 | opcode, 0x00]), "opcode")

    def test_control_frame_over_125_bytes(self):
        self.refuse(bytes([0x89, 0x7E, 0x00, 0x7E]) + b"p" * 126, "control")

    def test_fragmented_control_frame(self):
        self.refuse(bytes([0x09, 0x00]), "control")

    def test_frame_over_1_mib_is_refused_from_the_header(self):
        self.refuse(bytes([0x82, 0x7F]) + ((1 << 20) + 1).to_bytes(8, "big"), "1 MiB")
        self.refuse(bytes([0x82, 0x7F]) + (1 << 63).to_bytes(8, "big"), "1 MiB")
        self.assertIsNone(decode_frame(bytes([0x82, 0x7F]) + (1 << 20).to_bytes(8, "big")))

    def test_plain_ws_only_for_127_0_0_1_in_loopback_mode(self):
        for url, loop in (("ws://127.0.0.1:9/link?ticket=SECRET", False), ("ws://localhost:9/link?ticket=SECRET", True),
                          ("ws://127.0.0.1.invalid:9/link?ticket=SECRET", True), ("ws://wss-test.invalid/link?ticket=SECRET", True),
                          ("http://127.0.0.1:9/link?ticket=SECRET", True), ("ftp://127.0.0.1/?ticket=SECRET", True)):
            with self.subTest(url=url, loop=loop), self.assertRaises(Closed) as caught:
                WebSocket.connect(url, timeout=1, plain_loopback=loop)
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertIn("wss://", str(caught.exception))

    def test_open_url_refuses_a_plain_api_base_off_loopback(self):
        with self.assertRaises(Closed) as caught:
            open_url(TOKEN, "http://slack.invalid/api")
        self.assertNotIn(TOKEN, str(caught.exception))

    @unittest.skipUnless(shutil.which("openssl"), "openssl is needed to make a self-signed certificate")
    def test_wss_checks_the_certificate(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp / "k.pem"),
                        "-out", str(tmp / "c.pem"), "-days", "1", "-subj", "/CN=127.0.0.1"],
                       check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tmp / "c.pem", tmp / "k.pem")
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)

        def accept():
            conn, _ = listener.accept()
            try:
                context.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError):
                pass
            conn.close()

        threading.Thread(target=accept, daemon=True).start()
        with self.assertRaises(Closed) as caught:
            WebSocket.connect(f"wss://127.0.0.1:{listener.getsockname()[1]}/link?ticket=SECRET", timeout=5)
        self.assertIn("CertVerification", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))


class Pair(unittest.TestCase):
    """The client on one end of a socket pair, raw RFC bytes written on the other."""

    def setUp(self):
        self.peer, mine = socket.socketpair()
        self.peer.settimeout(5)
        self.ws = WebSocket(mine)
        self.addCleanup(self.peer.close)
        self.addCleanup(self.ws.close)
        self.reader = self.peer.makefile("rb")

    def test_joins_fragmented_hel_lo(self):
        self.peer.sendall(bytes([0x01, 0x03, 0x48, 0x65, 0x6C, 0x80, 0x02, 0x6C, 0x6F]))
        self.assertEqual(self.ws.recv_text(2), "Hello")

    def test_answers_ping_with_pong_carrying_the_payload(self):
        self.peer.sendall(bytes([0x89, 0x05]) + HELLO + bytes([0x81, 0x02]) + b"ok")
        self.assertEqual(self.ws.recv_text(2), "ok")
        self.assertEqual(read_client_frame(self.reader), (0xA, True, True, b"Hello"))

    def test_ping_between_fragments(self):
        self.peer.sendall(bytes([0x01, 0x03]) + b"Hel" + bytes([0x89, 0x00]) + bytes([0x80, 0x02]) + b"lo")
        self.assertEqual(self.ws.recv_text(2), "Hello")

    def test_send_text_is_masked(self):
        self.ws.send_text("Hello")
        self.assertEqual(read_client_frame(self.reader), (0x1, True, True, b"Hello"))

    def test_timeout_is_none(self):
        self.assertIsNone(self.ws.recv_text(0.05))

    def test_close_frame_raises_closed(self):
        self.peer.sendall(bytes([0x88, 0x02, 0x03, 0xE8]))
        with self.assertRaises(Closed):
            self.ws.recv_text(2)

    def test_eof_raises_closed(self):
        self.peer.shutdown(socket.SHUT_WR)
        with self.assertRaises(Closed):
            self.ws.recv_text(2)

    def test_continuation_without_a_first_frame(self):
        self.peer.sendall(bytes([0x80, 0x02]) + b"lo")
        with self.assertRaises(Closed):
            self.ws.recv_text(2)

    def test_message_over_1_mib_in_fragments(self):
        chunk = b"a" * (1 << 19)
        data = server_frame(0x1, chunk, fin=False) + server_frame(0x0, chunk, fin=False) + server_frame(0x0, b"a")
        threading.Thread(target=self.peer.sendall, args=(data,), daemon=True).start()
        with self.assertRaises(Closed):
            self.ws.recv_text(5)


class AgainstFake(unittest.TestCase):
    def setUp(self):
        self.slack = FakeSlack(token=TOKEN)
        self.addCleanup(self.slack.close)

    def test_open_url_with_the_token(self):
        url = open_url(TOKEN, self.slack.api_url)
        self.assertTrue(url.startswith(f"ws://127.0.0.1:{self.slack.port}/link?ticket="))
        self.assertIn(f"Bearer {TOKEN}", self.slack.requests[0])

    def test_open_url_auth_error_holds_only_the_code(self):
        with self.assertRaises(AuthError) as caught:
            open_url("xapp-WRONG-2", self.slack.api_url)
        self.assertEqual(str(caught.exception), "Slack connect: invalid_auth")

    def test_bad_accept_key_is_refused(self):
        self.slack.bad_accept = True
        url = open_url(TOKEN, self.slack.api_url)
        with self.assertRaises(Closed) as caught:
            WebSocket.connect(url, timeout=5, plain_loopback=True)
        self.assertIn("Sec-WebSocket-Accept", str(caught.exception))
        self.assertNotIn("ticket", str(caught.exception))

    def test_masked_frame_from_the_server_breaks_the_link(self):
        ws = WebSocket.connect(open_url(TOKEN, self.slack.api_url), timeout=5, plain_loopback=True)
        self.addCleanup(ws.close)
        self.assertIn("hello", ws.recv_text(5))
        self.slack.send_raw(server_frame(0x1, b"x", mask=b"abcd"))
        with self.assertRaises(Closed):
            ws.recv_text(5)


class Serve(unittest.TestCase):
    def setUp(self):
        self.slack = FakeSlack(token=TOKEN)
        self.addCleanup(self.slack.close)
        self.logs, self.envelopes, self.ticks, self.sleeps = [], [], [], []
        self.stop = threading.Event()

    def start(self, on_envelope=None, idle=30.0, sleep=None, token=TOKEN):
        def default(envelope, ack):
            self.envelopes.append(envelope)

        thread = threading.Thread(target=serve, daemon=True, kwargs=dict(
            token=token, on_envelope=on_envelope or default, tick=lambda: self.ticks.append(time.monotonic()),
            stop=self.stop, api_base=self.slack.api_url, idle=idle, log=self.log, sleep=sleep or time.sleep))
        thread.start()

        def finish():
            self.stop.set()
            thread.join(10)
            self.assertFalse(thread.is_alive(), "serve did not return after stop")
            self.assertNoSecrets()

        self.addCleanup(finish)
        return thread

    def log(self, line):
        with self.slack.cond:
            self.logs.append(line)
            self.slack.cond.notify_all()

    def assertNoSecrets(self):
        for line in self.logs:
            for secret in (TOKEN, "ticket=", "/link", str(self.slack.port)):
                self.assertNotIn(secret, line)

    def connected(self, count=1):
        self.slack.wait_for(lambda: self.logs.count("socket mode: connected") >= count)

    def test_hello_logs_connected_and_tick_runs(self):
        self.start()
        self.connected()
        self.slack.wait_for(lambda: len(self.ticks) >= 1)

    def test_auto_ack_after_the_callback_returns(self):
        seen_during = []

        def slow(envelope, ack):
            time.sleep(0.2)
            seen_during.append(self.slack.acked(envelope["envelope_id"]))

        self.start(slow)
        self.connected()
        eid = self.slack.send_interactive({"type": "block_actions"})
        self.slack.wait_for(lambda: self.slack.acked(eid) == 1)
        self.assertEqual(seen_during, [0])

    def test_ack_from_the_callback_is_on_the_wire_before_it_returns(self):
        def acks_first(envelope, ack):
            ack()
            self.slack.wait_for(lambda: self.slack.acked(envelope["envelope_id"]) == 1, timeout=5)
            self.envelopes.append(envelope)

        self.start(acks_first)
        self.connected()
        first = self.slack.send_interactive({"n": 1})
        second = self.slack.send_interactive({"n": 2})
        self.slack.wait_for(lambda: len(self.envelopes) == 2)
        time.sleep(0.2)
        self.assertEqual([self.slack.acked(first), self.slack.acked(second)], [1, 1])
        self.assertEqual([a["envelope_id"] for a in self.slack.acks], [first, second])

    def test_ack_order_follows_envelope_order_and_each_is_acked(self):
        self.start()
        self.connected()
        ids = [self.slack.send_interactive({"n": n}) for n in range(5)]
        self.slack.wait_for(lambda: len(self.slack.acks) == 5)
        self.assertEqual([a["envelope_id"] for a in self.slack.acks], ids)
        self.assertEqual([e["payload"]["n"] for e in self.envelopes], list(range(5)))

    def test_no_ack_when_the_callback_raises(self):
        def boom(envelope, ack):
            if envelope["payload"].get("bad"):
                raise RuntimeError(f"broken with {TOKEN}")
            self.envelopes.append(envelope)

        self.start(boom)
        self.connected()
        bad = self.slack.send_interactive({"bad": True})
        good = self.slack.send_interactive({"bad": False})
        self.slack.wait_for(lambda: self.slack.acked(good) == 1)
        self.assertEqual(self.slack.acked(bad), 0)
        self.assertTrue(any("not acked" in line for line in self.logs))

    def test_redelivery_is_acked_again(self):
        self.start()
        self.connected()
        eid = self.slack.send_interactive({"n": 1})
        self.slack.wait_for(lambda: self.slack.acked(eid) == 1)
        self.slack.redeliver(eid)
        self.slack.wait_for(lambda: self.slack.acked(eid) == 2)
        self.assertEqual([e["retry_attempt"] for e in self.envelopes], [0, 1])

    def test_messages_without_envelope_id_are_not_passed_on(self):
        self.start()
        self.connected()
        self.slack.send_raw(server_frame(0x1, b"not json"))
        self.slack.send_json({"type": "events_api"})
        eid = self.slack.send_interactive({"n": 1})
        self.slack.wait_for(lambda: self.slack.acked(eid) == 1)
        self.assertEqual(len(self.envelopes), 1)

    def test_disconnect_opens_a_new_link(self):
        self.start()
        self.connected()
        self.slack.disconnect()
        self.slack.wait_for(lambda: self.slack.opens == 2)
        self.connected(2)
        eid = self.slack.send_interactive({"after": "disconnect"})
        self.slack.wait_for(lambda: self.slack.acked(eid) == 1)
        self.assertEqual(self.slack.acks[-1]["link"], 2)

    def test_reconnect_after_a_drop(self):
        self.start()
        self.connected()
        self.slack.drop()
        self.slack.wait_for(lambda: self.slack.opens == 2)
        self.connected(2)
        eid = self.slack.send_interactive({"after": "drop"})
        self.slack.wait_for(lambda: self.slack.acked(eid) == 1)

    def test_idle_link_is_pinged_and_kept_when_pong_comes(self):
        self.start(idle=0.3)
        self.connected()
        self.slack.wait_for(lambda: any(f[1] == 0x9 for f in self.slack.frames))
        time.sleep(1.0)
        self.assertEqual(self.slack.opens, 1)
        self.assertTrue(all(f[3] for f in self.slack.frames), "every client frame is masked")

    def test_ping_timeout_is_treated_as_a_drop(self):
        self.slack.answer_pings = False
        self.start(idle=0.3)
        self.connected()
        started = time.monotonic()
        self.slack.wait_for(lambda: self.slack.opens == 2, timeout=5)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(any("no frame for" in line for line in self.logs))
        self.assertTrue(any(f[1] == 0x9 for f in self.slack.frames))

    def test_server_ping_gets_a_pong_with_its_payload(self):
        self.start()
        self.connected()
        self.slack.ping(b"are-you-there")
        self.slack.wait_for(lambda: self.slack.pongs == [b"are-you-there"])

    def test_auth_error_backs_off_600_s_in_ticks(self):
        def sleep(seconds):
            self.sleeps.append(seconds)
            if sum(self.sleeps) >= 600:
                self.stop.set()

        self.slack.auth_error = "invalid_auth"
        self.start(sleep=sleep)
        self.slack.wait_for(lambda: self.stop.is_set())
        self.assertEqual(sum(self.sleeps), 600)
        self.assertTrue(all(s <= 5 for s in self.sleeps))
        self.assertGreaterEqual(len(self.ticks), len(self.sleeps))
        self.assertEqual(self.logs[0], "socket mode: Slack connect: invalid_auth; retry in 600 s")

    def test_other_errors_back_off_doubling_to_60_s(self):
        def sleep(seconds):
            self.sleeps.append(seconds)
            if sum(1 for line in self.logs if "retry in" in line) >= 8:
                self.stop.set()

        self.slack.auth_error = "internal_error"
        self.start(sleep=sleep)
        self.slack.wait_for(lambda: self.stop.is_set())
        delays = [int(line.split("retry in ")[1].split()[0]) for line in self.logs if "retry in" in line]
        self.assertEqual(delays[:8], [1, 2, 4, 8, 16, 32, 60, 60])

    def test_token_and_link_never_in_logs_or_exceptions(self):
        self.start(token="xapp-WRONG-2", sleep=lambda s: self.stop.set())
        self.slack.wait_for(lambda: self.stop.is_set())
        self.slack.bad_accept = True
        url = open_url(TOKEN, self.slack.api_url)
        errors = []
        for call in (lambda: WebSocket.connect(url, timeout=5, plain_loopback=True),
                     lambda: WebSocket.connect(url.replace("ws://", "wss://"), timeout=5),
                     lambda: open_url(TOKEN, f"http://127.0.0.1:{self.slack.port}/nowhere"),
                     lambda: open_url(TOKEN, "http://127.0.0.1:1/api")):
            with self.assertRaises(Closed) as caught:
                call()
            errors.append(caught.exception)
        for e in errors:
            self.assertIsNone(e.__cause__)
            self.assertTrue(e.__context__ is None or e.__suppress_context__)
            for secret in (TOKEN, "xapp-WRONG-2", "ticket", "/link"):
                self.assertNotIn(secret, str(e))
                self.assertNotIn(secret, repr(e))
        self.assertTrue(self.logs)


if __name__ == "__main__":
    unittest.main()

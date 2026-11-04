"""A fake Slack on 127.0.0.1: apps.connections.open, the Socket Mode WebSocket, response_url and the webhook.

The frame code here is written apart from harness/socket_mode.py on purpose, so the client and the fake do
not share one wrong idea of RFC 6455.

    slack = FakeSlack(token="xapp-CANARY-1")      # starts serving at once
    slack.api_url                                 # http://127.0.0.1:<port>/api
    slack.hook_url                                # http://127.0.0.1:<port>/hook
    slack.send_interactive(payload)               # returns the envelope id
    slack.wait_for(lambda: slack.acked("e1"))
    slack.close()
"""
import base64
import hashlib
import itertools
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode() + MAGIC).digest()).decode()


def server_frame(opcode: int, data: bytes, fin: bool = True, rsv: int = 0, mask: bytes = b"") -> bytes:
    """A frame as a server writes it. mask, rsv and fin exist so a test can send frames the client refuses."""
    first = (0x80 if fin else 0) | (rsv << 4) | opcode
    size = len(data)
    if size <= 125:
        length = bytes([size])
    elif size <= 0xFFFF:
        length = bytes([126]) + size.to_bytes(2, "big")
    else:
        length = bytes([127]) + size.to_bytes(8, "big")
    if mask:
        length = bytes([length[0] | 0x80]) + length[1:]
        data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
    return bytes([first]) + length + mask + data


def read_client_frame(stream):
    """(opcode, fin, masked, payload) from a file-like stream, or None at EOF."""
    head = stream.read(2)
    if len(head) < 2:
        return None
    fin, opcode = bool(head[0] & 0x80), head[0] & 0x0F
    masked, size = bool(head[1] & 0x80), head[1] & 0x7F
    if size == 126:
        size = int.from_bytes(stream.read(2), "big")
    elif size == 127:
        size = int.from_bytes(stream.read(8), "big")
    key = stream.read(4) if masked else b""
    data = stream.read(size)
    if len(data) < size:
        return None
    if masked:
        data = bytes(b ^ key[i & 3] for i, b in enumerate(data))
    return opcode, fin, masked, data


class Link:
    """One WebSocket connection. The test thread writes through send; the handler thread reads."""

    def __init__(self, number, conn):
        self.number, self.conn, self.lock, self.alive = number, conn, threading.Lock(), True

    def send(self, data: bytes) -> None:
        with self.lock:
            try:
                self.conn.sendall(data)
            except OSError:
                self.alive = False

    def drop(self) -> None:
        self.alive = False
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class FakeSlack:
    def __init__(self, token="xapp-CANARY-1", hello=True):
        self.token, self.hello = token, hello
        self.cond = threading.Condition()
        self.opens = 0
        self.auth_error = None
        self.answer_pings = True
        self.bad_accept = False
        self.respond_status = 200
        self.links: list[Link] = []
        self.acks: list[dict] = []
        self.frames: list[tuple] = []
        self.pongs: list[bytes] = []
        self.responses: list[tuple[str, dict]] = []
        self.hooks: list[dict] = []
        self.requests: list[str] = []
        self.sent: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.05,), daemon=True)
        self._thread.start()

    @property
    def api_url(self):
        return f"http://127.0.0.1:{self.port}/api"

    @property
    def hook_url(self):
        return f"http://127.0.0.1:{self.port}/hook"

    def response_url(self, name="r1"):
        return f"http://127.0.0.1:{self.port}/respond/{name}"

    def close(self):
        for link in list(self.links):
            link.drop()
        self._server.shutdown()
        self._server.server_close()

    def wait_for(self, predicate, timeout=20.0):
        deadline = time.monotonic() + timeout
        with self.cond:
            while not predicate():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError("fake Slack: condition not met in time")
                self.cond.wait(min(left, 0.05))

    def _note(self, attr, value):
        with self.cond:
            getattr(self, attr).append(value)
            self.cond.notify_all()

    @property
    def link(self) -> Link:
        live = [link for link in self.links if link.alive]
        if not live:
            raise AssertionError("fake Slack: no live connection")
        return live[-1]

    def acked(self, envelope_id) -> int:
        return sum(1 for a in self.acks if a["envelope_id"] == envelope_id)

    def send_raw(self, data: bytes) -> None:
        self.link.send(data)

    def send_json(self, obj) -> None:
        self.link.send(server_frame(0x1, json.dumps(obj).encode()))

    def send_envelope(self, kind, payload, envelope_id=None) -> str:
        envelope_id = envelope_id or f"e{next(self._ids)}"
        envelope = {"envelope_id": envelope_id, "type": kind, "payload": payload,
                    "accepts_response_payload": False, "retry_attempt": 0, "retry_reason": ""}
        self.sent[envelope_id] = envelope
        self.send_json(envelope)
        return envelope_id

    def send_interactive(self, payload, envelope_id=None) -> str:
        return self.send_envelope("interactive", payload, envelope_id)

    def redeliver(self, envelope_id) -> None:
        envelope = dict(self.sent[envelope_id])
        envelope["retry_attempt"] += 1
        envelope["retry_reason"] = "timeout"
        self.sent[envelope_id] = envelope
        self.send_json(envelope)

    def disconnect(self, reason="refresh_requested") -> None:
        self.send_json({"type": "disconnect", "reason": reason, "debug_info": {"host": "fake"}})

    def ping(self, data=b"fake-ping") -> None:
        self.link.send(server_frame(0x9, data))

    def drop(self) -> None:
        self.link.drop()

    def click_payload(self, verb, value, user="U0TESTAPPROVER", response_name="r1", action_ts=None):
        """A block_actions payload shaped like Slack's for one button."""
        return {"type": "block_actions", "user": {"id": user, "username": "reviewer-login"},
                "api_app_id": "A0TESTAPP", "container": {"type": "message", "message_ts": "1.000001"},
                "response_url": self.response_url(response_name),
                "actions": [{"type": "button", "action_id": verb, "block_id": "news",
                             "value": str(value), "action_ts": action_ts or f"{time.time():.6f}"}]}

    def click_from_post(self, post, verb, user="U0TESTAPPROVER", response_name="r1"):
        """The click payload for the button with action_id verb in a posted message (a webhook body)."""
        for block in post.get("blocks", []):
            for element in block.get("elements", []) if block.get("type") == "actions" else []:
                if element.get("action_id") == verb:
                    return self.click_payload(verb, element["value"], user, response_name)
        raise AssertionError(f"fake Slack: no {verb} button in the post")

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _answer(self, status, obj=None):
                data = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self._body()
                fake._note("requests", f"POST {self.path}\n{dict(self.headers)}\n{body.decode('utf-8', 'replace')}")
                if self.path == "/api/apps.connections.open":
                    with fake.cond:
                        fake.opens += 1
                        number = fake.opens
                        fake.cond.notify_all()
                    if fake.auth_error or self.headers.get("Authorization") != f"Bearer {fake.token}":
                        return self._answer(200, {"ok": False, "error": fake.auth_error or "invalid_auth"})
                    return self._answer(200, {"ok": True, "url": f"ws://127.0.0.1:{fake.port}/link?ticket=t{number}"})
                parsed = json.loads(body or b"{}")
                if self.path.startswith("/respond/"):
                    fake._note("responses", (self.path.split("/")[-1], parsed))
                    return self._answer(fake.respond_status, {"ok": fake.respond_status == 200})
                if self.path == "/hook":
                    fake._note("hooks", parsed)
                    return self._answer(200)
                self._answer(404)

            def do_GET(self):
                fake._note("requests", f"GET {self.path}\n{dict(self.headers)}")
                if not self.path.startswith("/link") or self.headers.get("Upgrade", "").lower() != "websocket":
                    return self._answer(404)
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = "AAAA" if fake.bad_accept else ws_accept(key)
                self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                  f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                self.wfile.flush()
                self.close_connection = True
                with fake.cond:
                    link = Link(len(fake.links) + 1, self.connection)
                    fake.links.append(link)
                    fake.cond.notify_all()
                if fake.hello:
                    link.send(server_frame(0x1, json.dumps({"type": "hello", "num_connections": 1}).encode()))
                self._serve_link(link)

            def _serve_link(self, link):
                while link.alive:
                    try:
                        frame = read_client_frame(self.rfile)
                    except OSError:
                        frame = None
                    if frame is None:
                        break
                    opcode, fin, masked, data = frame
                    fake._note("frames", (link.number, opcode, fin, masked, data))
                    if opcode == 0x1:
                        try:
                            msg = json.loads(data)
                        except ValueError:
                            continue
                        if isinstance(msg, dict) and "envelope_id" in msg and "type" not in msg:
                            fake._note("acks", {**msg, "link": link.number, "at": time.monotonic()})
                    elif opcode == 0x9 and fake.answer_pings:
                        link.send(server_frame(0xA, data))
                    elif opcode == 0xA:
                        fake._note("pongs", data)
                    elif opcode == 0x8:
                        link.send(server_frame(0x8, data[:2]))
                        break
                link.alive = False
                with fake.cond:
                    fake.cond.notify_all()

        return Handler

"""A small Slack Socket Mode client over a stdlib WebSocket (RFC 6455). It knows nothing about news.

The wss URL that apps.connections.open returns is a secret, like the xapp- token: neither is ever logged or
put in an exception."""
import base64
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

SLACK_API = "https://slack.com/api"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 1 << 20
OPCODES = {0x0, 0x1, 0x2, 0x8, 0x9, 0xA}
TICK = 5.0
MAX_BACKOFF = 60
AUTH_BACKOFF = 600
AUTH_ERRORS = {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired",
               "not_allowed_token_type", "missing_scope"}
ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")


class Closed(Exception):
    """The link is closed or broken. Never holds the URL."""


class AuthError(Closed):
    """apps.connections.open refused the token. Holds only Slack's error code."""


def accept_key(key: str) -> str:
    """Sec-WebSocket-Accept for a Sec-WebSocket-Key (RFC 6455 section 1.3)."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def _xor(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes, mask: bytes | None) -> bytes:
    """One final frame, masked with the 4 byte mask, or unmasked when mask is None. The client always masks."""
    bit = 0x80 if mask is not None else 0
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, bit | n)
    elif n < 1 << 16:
        head = struct.pack("!BBH", 0x80 | opcode, bit | 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, bit | 127, n)
    if mask is None:
        return head + payload
    return head + mask + _xor(payload, mask)


def decode_frame(buf: bytes) -> tuple[int, bool, bytes, int] | None:
    """(opcode, fin, payload, bytes used) or None when buf holds no whole frame. Raises Closed on a masked
    server frame, an RSV bit, an unknown opcode, a control frame over 125 bytes, or a frame over 1 MiB."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    fin, opcode = bool(b0 & 0x80), b0 & 0x0F
    if b0 & 0x70:
        raise Closed("frame with an RSV bit set")
    if opcode not in OPCODES:
        raise Closed(f"frame with unknown opcode {opcode}")
    if b1 & 0x80:
        raise Closed("masked frame from the server")
    n, pos = b1 & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, pos = struct.unpack_from("!H", buf, 2)[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, pos = struct.unpack_from("!Q", buf, 2)[0], 10
    if opcode >= 0x8 and (n > 125 or not fin):
        raise Closed("control frame over 125 bytes or fragmented")
    if n > MAX_FRAME:
        raise Closed("frame over 1 MiB")
    if len(buf) < pos + n:
        return None
    return opcode, fin, bytes(buf[pos:pos + n]), pos + n


def _connect_error(e: BaseException) -> Closed:
    return Closed(f"connect failed: {type(e).__name__}")


class WebSocket:
    def __init__(self, sock):
        self._sock = sock
        self._buf = b""
        self._parts: list[bytes] | None = None
        self._kind = 0
        self._closed = False
        self.hello = False
        self.last_frame = time.monotonic()

    @classmethod
    def connect(cls, url: str, timeout: float = 10, plain_loopback: bool = False) -> "WebSocket":
        """wss only, with ssl.create_default_context(). ws:// only for 127.0.0.1 when plain_loopback.
        Raises Closed on a bad accept key."""
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme == "wss":
            secure = True
        elif parts.scheme == "ws" and plain_loopback and host == "127.0.0.1":
            secure = False
        else:
            raise Closed("only a wss:// link is allowed")
        try:
            port = parts.port or (443 if secure else 80)
            sock = socket.create_connection((host, port), timeout=timeout)
            if secure:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        except (OSError, ValueError) as e:
            raise _connect_error(e) from None
        ws = cls(sock)
        try:
            ws._handshake(host if parts.port is None else f"{host}:{parts.port}",
                          (parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
        except BaseException:
            ws._sock.close()
            raise
        return ws

    def _handshake(self, host_header: str, target: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (f"GET {target} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        try:
            self._sock.sendall(request.encode("ascii"))
            while b"\r\n\r\n" not in self._buf:
                if len(self._buf) > 16384:
                    raise Closed("handshake answer too long")
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise Closed("connection closed during the handshake")
                self._buf += chunk
        except OSError as e:
            raise _connect_error(e) from None
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ")
        if len(status) < 2 or status[1] != "101":
            raise Closed(f"handshake refused: status {status[1] if len(status) > 1 else 'missing'}")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if headers.get("upgrade", "").lower() != "websocket" or "upgrade" not in headers.get("connection", "").lower():
            raise Closed("handshake answer is not a WebSocket upgrade")
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise Closed("handshake answer has a bad Sec-WebSocket-Accept")

    def _send(self, opcode: int, payload: bytes) -> None:
        try:
            self._sock.settimeout(10)
            self._sock.sendall(encode_frame(opcode, payload, os.urandom(4)))
        except OSError as e:
            raise Closed(f"send failed: {type(e).__name__}") from None

    def send_text(self, text: str) -> None:
        self._send(0x1, text.encode("utf-8"))

    def ping(self) -> None:
        self._send(0x9, b"harness")

    def recv_text(self, timeout: float) -> str | None:
        """Next text message, or None on timeout. Joins continuation frames, answers ping with pong.
        Raises Closed on close, EOF or a bad frame."""
        deadline = time.monotonic() + timeout
        while True:
            frame = decode_frame(self._buf)
            if frame is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                try:
                    self._sock.settimeout(left)
                    chunk = self._sock.recv(65536)
                except socket.timeout:
                    return None
                except OSError as e:
                    raise Closed(f"read failed: {type(e).__name__}") from None
                if not chunk:
                    raise Closed("connection closed by the server")
                self._buf += chunk
                continue
            opcode, fin, payload, used = frame
            self._buf = self._buf[used:]
            self.last_frame = time.monotonic()
            text = self._handle(opcode, fin, payload)
            if text is not None:
                return text

    def _handle(self, opcode: int, fin: bool, payload: bytes) -> str | None:
        if opcode == 0x9:
            self._send(0xA, payload)
            return None
        if opcode == 0xA:
            return None
        if opcode == 0x8:
            code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
            self.close()
            raise Closed(f"closed by the server (code {code})")
        if opcode == 0x0:
            if self._parts is None:
                raise Closed("continuation frame without a first frame")
            self._parts.append(payload)
        else:
            if self._parts is not None:
                raise Closed("new message inside a fragmented message")
            self._parts, self._kind = [payload], opcode
        if sum(map(len, self._parts)) > MAX_FRAME:
            raise Closed("message over 1 MiB")
        if not fin:
            return None
        data, kind, self._parts = b"".join(self._parts), self._kind, None
        if kind != 0x1:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise Closed("text message is not UTF-8") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.sendall(encode_frame(0x8, struct.pack("!H", 1000), os.urandom(4)))
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def _loopback(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "http" and parts.hostname == "127.0.0.1"


def open_url(token: str, api_base: str = SLACK_API) -> str:
    """POST apps.connections.open with the bearer token. Returns the wss URL. Errors carry only Slack's
    error code."""
    if not (urlsplit(api_base).scheme == "https" or _loopback(api_base)):
        raise Closed("apps.connections.open: the API base must be https")
    request = urllib.request.Request(api_base.rstrip("/") + "/apps.connections.open", data=b"", method="POST",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as res:
            data = json.loads(res.read())
    except urllib.error.HTTPError as e:
        raise Closed(f"apps.connections.open: HTTP {e.code}") from None
    except (OSError, ValueError) as e:
        raise Closed(f"apps.connections.open: {type(e).__name__}") from None
    if not isinstance(data, dict):
        raise Closed("apps.connections.open: answer is not an object")
    if data.get("ok") is not True:
        code = data.get("error")
        code = code if isinstance(code, str) and ERROR_CODE.fullmatch(code) else "unknown_error"
        raise (AuthError if code in AUTH_ERRORS else Closed)(f"apps.connections.open: {code}")
    url = data.get("url")
    if not isinstance(url, str) or not url:
        raise Closed("apps.connections.open: answer has no url")
    return url


def _wait(seconds: float, tick, stop, sleep) -> None:
    left = seconds
    while left > 0 and not stop.is_set():
        tick()
        step = min(TICK, left)
        sleep(step)
        left -= step


def _read(ws: WebSocket, on_envelope, tick, stop, idle: float, log) -> None:
    """Read envelopes until stop or a disconnect envelope. Raises Closed."""
    pinged, next_tick = False, 0.0
    while not stop.is_set():
        now = time.monotonic()
        if now >= next_tick:
            tick()
            next_tick = now + TICK
        quiet = now - ws.last_frame
        if quiet >= 2 * idle:
            raise Closed(f"no frame for {2 * idle:g} s")
        if quiet >= idle and not pinged:
            ws.ping()
            pinged = True
        elif quiet < idle:
            pinged = False
        text = ws.recv_text(min(1.0, idle / 2))
        if text is None:
            continue
        try:
            envelope = json.loads(text)
        except ValueError:
            log("socket mode: a message that is not JSON was ignored")
            continue
        if not isinstance(envelope, dict):
            log("socket mode: a message that is not an object was ignored")
            continue
        kind = envelope.get("type")
        if kind == "hello":
            ws.hello = True
            log("socket mode: connected")
            continue
        if kind == "disconnect":
            reason = envelope.get("reason")
            reason = reason if isinstance(reason, str) and ERROR_CODE.fullmatch(reason) else "no reason"
            log(f"socket mode: Slack asked to reconnect ({reason})")
            return
        envelope_id = envelope.get("envelope_id")
        if not isinstance(envelope_id, str) or not envelope_id:
            continue
        sent = []

        def ack(payload=None, envelope_id=envelope_id, sent=sent):
            if sent:
                return
            sent.append(True)
            body = {"envelope_id": envelope_id}
            if payload is not None:
                body["payload"] = payload
            ws.send_text(json.dumps(body))

        try:
            on_envelope(envelope, ack)
        except Closed:
            raise
        except Exception as e:
            log(f"socket mode: the envelope handler failed ({type(e).__name__}); not acked, Slack sends it again")
            continue
        ack()


def serve(token: str, on_envelope, tick, stop, api_base: str = SLACK_API, idle: float = 30,
          log=print, sleep=time.sleep) -> None:
    """Connect, read and reconnect until stop is set. One thread is the only socket writer.
    hello: log "connected" (no URL). disconnect: reconnect now.
    An envelope with envelope_id: on_envelope(envelope, ack); acked after it returns if it did not ack; if it
    raises, no ack, so Slack sends it again.
    tick() at least every 5 s. No frame for idle s: ping; still nothing for idle s more: reconnect.
    Errors back off 1, 2, 4 ... 60 s; an auth error from apps.connections.open backs off 600 s."""
    plain = _loopback(api_base)
    failures = 0
    while not stop.is_set():
        try:
            ws = WebSocket.connect(open_url(token, api_base), plain_loopback=plain)
        except AuthError as e:
            log(f"socket mode: {e}; retry in {AUTH_BACKOFF} s")
            _wait(AUTH_BACKOFF, tick, stop, sleep)
            continue
        except Closed as e:
            delay = min(MAX_BACKOFF, 2 ** failures)
            failures += 1
            log(f"socket mode: {e}; retry in {delay} s")
            _wait(delay, tick, stop, sleep)
            continue
        try:
            _read(ws, on_envelope, tick, stop, idle, log)
            reason = None
        except Closed as e:
            reason = str(e)
        finally:
            ws.close()
        if reason is None:
            failures = 0
            continue
        if ws.hello:
            failures = 0
            log(f"socket mode: link lost ({reason}); reconnecting")
            continue
        delay = min(MAX_BACKOFF, 2 ** failures)
        failures += 1
        log(f"socket mode: link lost before hello ({reason}); retry in {delay} s")
        _wait(delay, tick, stop, sleep)

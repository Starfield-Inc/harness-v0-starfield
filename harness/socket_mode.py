"""A small Slack Socket Mode client over a stdlib WebSocket (RFC 6455). It knows nothing about news.

The wss URL that apps.connections.open returns is a secret, like the xapp- token: neither is ever logged or
put in an exception."""
import time

SLACK_API = "https://slack.com/api"


class Closed(Exception):
    """The link is closed or broken. Never holds the URL."""


def accept_key(key: str) -> str:
    """Sec-WebSocket-Accept for a Sec-WebSocket-Key (RFC 6455 section 1.3)."""
    raise NotImplementedError


def encode_frame(opcode: int, payload: bytes, mask: bytes | None) -> bytes:
    """One final frame, masked with the 4 byte mask, or unmasked when mask is None. The client always masks."""
    raise NotImplementedError


def decode_frame(buf: bytes) -> tuple[int, bool, bytes, int] | None:
    """(opcode, fin, payload, bytes used) or None when buf holds no whole frame. Raises Closed on a masked
    server frame, an RSV bit, an unknown opcode, a control frame over 125 bytes, or a frame over 1 MiB."""
    raise NotImplementedError


class WebSocket:
    @classmethod
    def connect(cls, url: str, timeout: float = 10, plain_loopback: bool = False) -> "WebSocket":
        """wss only, with ssl.create_default_context(). ws:// only for 127.0.0.1 when plain_loopback.
        Raises Closed on a bad accept key."""
        raise NotImplementedError

    def send_text(self, text: str) -> None:
        raise NotImplementedError

    def recv_text(self, timeout: float) -> str | None:
        """Next text message, or None on timeout. Joins continuation frames, answers ping with pong.
        Raises Closed on close, EOF or a bad frame."""
        raise NotImplementedError

    def ping(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def open_url(token: str, api_base: str = SLACK_API) -> str:
    """POST apps.connections.open with the bearer token. Returns the wss URL. Errors carry only Slack's
    error code."""
    raise NotImplementedError


def serve(token: str, on_envelope, tick, stop, api_base: str = SLACK_API, idle: float = 30,
          log=print, sleep=time.sleep) -> None:
    """Connect, read and reconnect until stop is set. One thread is the only socket writer.
    hello: log "connected" (no URL). disconnect: reconnect now.
    An envelope with envelope_id: on_envelope(envelope, ack); acked after it returns if it did not ack; if it
    raises, no ack, so Slack sends it again.
    tick() at least every 5 s. No frame for idle s: ping; still nothing for idle s more: reconnect.
    Errors back off 1, 2, 4 ... 60 s; an auth error from apps.connections.open backs off 600 s."""
    raise NotImplementedError

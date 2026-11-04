"""Wait for the network before the daily job fetches anything.

A login run can start before Wi-Fi is up. Every web fetch then fails with a DNS error.
"""
import socket
import time

DEFAULT_WAIT_SECONDS = 600
RETRY_SECONDS = 15
PROBE_HOST = "github.com"


def dns_probe() -> bool:
    try:
        socket.getaddrinfo(PROBE_HOST, 443)
    except OSError:
        return False
    return True


def wait_seconds(env) -> int:
    try:
        return max(0, int(env.get("HARNESS_NETWORK_WAIT_SECONDS", "").strip() or DEFAULT_WAIT_SECONDS))
    except ValueError:
        return DEFAULT_WAIT_SECONDS


def wait_for_network(env, sleep=time.sleep, now=time.monotonic, probe=dns_probe) -> bool:
    """True when the probe works. False after the wait runs out. A wait of 0 skips it and returns True."""
    limit = wait_seconds(env)
    if limit == 0:
        return True
    start = now()
    while True:
        try:
            if probe():
                return True
        except Exception:
            pass
        if now() - start + RETRY_SECONDS > limit:
            return False
        sleep(RETRY_SECONDS)

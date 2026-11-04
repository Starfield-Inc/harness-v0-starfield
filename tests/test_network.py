import socket
import unittest
from unittest import mock

from harness import network


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class WaitForNetworkTest(unittest.TestCase):
    def wait(self, env, results):
        clock, calls = Clock(), []

        def probe():
            calls.append(clock.t)
            result = results[min(len(calls) - 1, len(results) - 1)]
            if isinstance(result, Exception):
                raise result
            return result
        ok = network.wait_for_network(env, sleep=clock.sleep, now=clock.now, probe=probe)
        return ok, clock, calls

    def test_up_at_once_does_not_sleep(self):
        ok, clock, calls = self.wait({}, [True])
        self.assertTrue(ok)
        self.assertEqual((clock.sleeps, len(calls)), ([], 1))

    def test_retries_every_15_seconds_until_it_works(self):
        ok, clock, calls = self.wait({}, [False, False, True])
        self.assertTrue(ok)
        self.assertEqual(clock.sleeps, [15, 15])

    def test_gives_up_after_the_limit(self):
        ok, clock, _ = self.wait({"HARNESS_NETWORK_WAIT_SECONDS": "60"}, [False])
        self.assertFalse(ok)
        self.assertEqual(sum(clock.sleeps), 60)

    def test_default_limit_is_600_seconds(self):
        ok, clock, _ = self.wait({}, [False])
        self.assertFalse(ok)
        self.assertEqual(sum(clock.sleeps), 600)

    def test_zero_turns_the_wait_off(self):
        ok, clock, calls = self.wait({"HARNESS_NETWORK_WAIT_SECONDS": "0"}, [False])
        self.assertTrue(ok)
        self.assertEqual((clock.sleeps, calls), ([], []))

    def test_a_bad_limit_uses_the_default(self):
        ok, clock, _ = self.wait({"HARNESS_NETWORK_WAIT_SECONDS": "soon"}, [False])
        self.assertFalse(ok)
        self.assertEqual(sum(clock.sleeps), 600)

    def test_a_probe_that_raises_counts_as_down(self):
        ok, clock, _ = self.wait({"HARNESS_NETWORK_WAIT_SECONDS": "30"}, [RuntimeError("x")])
        self.assertFalse(ok)


class DnsProbeTest(unittest.TestCase):
    def test_lookup_error_is_false(self):
        with mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror("nodename nor servname provided")):
            self.assertFalse(network.dns_probe())

    def test_lookup_of_github_is_true(self):
        with mock.patch.object(socket, "getaddrinfo", return_value=[object()]) as look:
            self.assertTrue(network.dns_probe())
        self.assertEqual(look.call_args.args[0], "github.com")


if __name__ == "__main__":
    unittest.main()

"""Tests for UDP relaying: the `udp_forward` setting and the forwarding sockets."""

import json
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
import relay


def receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    return sock


class UdpForwardSettingTest(unittest.TestCase):

    def load(self, value):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"udp_forward": value}, f)
            with mock.patch.object(main, "_warn") as warn:
                settings = main.load_settings(path)
        return settings.udp_forward, warn.call_count

    def test_default_is_empty(self):
        self.assertEqual(main.Settings().udp_forward, ())

    def test_parses_host_port_strings(self):
        targets, warnings = self.load(["127.0.0.1:27778", "64.132.0.51:3000", "tv.lan:20777"])
        self.assertEqual(targets, (("127.0.0.1", 27778), ("64.132.0.51", 3000),
                                   ("tv.lan", 20777)))
        self.assertEqual(warnings, 0)

    def test_skips_malformed_entries(self):
        bad = ["127.0.0.1", ":3000", "127.0.0.1:", "127.0.0.1:0", "127.0.0.1:65536",
               "127.0.0.1:port", 3000, None, {"host": "127.0.0.1"}]
        targets, warnings = self.load(bad + ["10.0.0.2:3000"])
        self.assertEqual(targets, (("10.0.0.2", 3000),))
        self.assertEqual(warnings, len(bad))

    def test_drops_duplicates(self):
        targets, warnings = self.load(["10.0.0.2:3000", "10.0.0.2:3000"])
        self.assertEqual(targets, (("10.0.0.2", 3000),))
        self.assertEqual(warnings, 1)

    def test_not_a_list(self):
        targets, warnings = self.load("127.0.0.1:3000")
        self.assertEqual(targets, ())
        self.assertEqual(warnings, 1)


class OpenForwardersTest(unittest.TestCase):

    def test_copies_bytes_exactly_to_every_destination(self):
        a, b = receiver(), receiver()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        targets = [("127.0.0.1", a.getsockname()[1]), ("localhost", b.getsockname()[1])]
        socks, warnings = relay.open_forwarders(targets, listen_port=20777)
        for s in socks:
            self.addCleanup(s.close)
        self.assertEqual(warnings, [])
        self.assertEqual(len(socks), 2)

        payload = bytes(range(256)) * 5 + b"\x00"  # 1281 bytes, every byte value
        for s in socks:
            s.send(payload)
        self.assertEqual(a.recv(4096), payload)
        self.assertEqual(b.recv(4096), payload)

    def test_sockets_never_block(self):
        a = receiver()
        self.addCleanup(a.close)
        socks, _ = relay.open_forwarders([("127.0.0.1", a.getsockname()[1])], listen_port=20777)
        self.addCleanup(socks[0].close)
        self.assertFalse(socks[0].getblocking())

    def test_refuses_own_port_on_loopback(self):
        for host in ("127.0.0.1", "localhost", "127.0.0.2", "0.0.0.0"):
            socks, warnings = relay.open_forwarders([(host, 20777)], listen_port=20777)
            self.assertEqual(socks, [], host)
            self.assertEqual(len(warnings), 1, host)

    def test_refuses_own_port_on_lan_address(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: route lookup only, sends nothing
            lan_ip = probe.getsockname()[0]
        except OSError:
            self.skipTest("no route off this machine")
        finally:
            probe.close()
        socks, warnings = relay.open_forwarders([(lan_ip, 20777)], listen_port=20777)
        self.assertEqual(socks, [])
        self.assertEqual(len(warnings), 1)

    def test_other_port_on_this_machine_is_fine(self):
        a = receiver()
        self.addCleanup(a.close)
        socks, warnings = relay.open_forwarders([("127.0.0.1", a.getsockname()[1])],
                                                listen_port=20777)
        self.addCleanup(socks[0].close)
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()

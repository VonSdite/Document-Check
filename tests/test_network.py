import unittest
from unittest.mock import patch

from app import network


class NetworkAccessUrlsTest(unittest.TestCase):
    def test_specific_host_returns_single_url(self):
        self.assertEqual(network.access_urls("127.0.0.1", 5000), ["http://127.0.0.1:5000"])

    def test_wildcard_host_lists_loopback_and_local_ipv4_addresses(self):
        with (
            patch.object(network, "_interface_ipv4_addresses", return_value=["172.18.0.2", "127.0.0.1"]),
            patch.object(network, "_hostname_ipv4_addresses", return_value=["10.0.0.8", "172.18.0.2"]),
        ):
            urls = network.access_urls("0.0.0.0", 5000)

        self.assertEqual(
            urls,
            [
                "http://127.0.0.1:5000",
                "http://172.18.0.2:5000",
                "http://10.0.0.8:5000",
            ],
        )


if __name__ == "__main__":
    unittest.main()

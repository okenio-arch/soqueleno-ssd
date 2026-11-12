import ipaddress
import unittest
from unittest.mock import patch

import httpx

from sdi.crawler import CrawlBlocked, bounded_get


class _Peer:
    def __init__(self, address):
        self.address = address

    def get_extra_info(self, name):
        return (self.address, 443) if name == "server_addr" else None


class BoundedGetTests(unittest.TestCase):
    def setUp(self):
        self.public_ip = ipaddress.ip_address("93.184.216.34")

    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    def _patch_dns(self):
        return patch.multiple(
            "sdi.crawler",
            validate_public_url=lambda value: value,
            _resolve_public_addresses=lambda host: {self.public_ip},
        )

    def test_streamed_body_limit_is_enforced(self):
        def handler(request):
            return httpx.Response(
                200, headers={"content-type": "text/html"},
                content=b"x" * 32, request=request,
                extensions={"network_stream": _Peer(str(self.public_ip))},
            )

        with self._patch_dns(), self._client(handler) as client:
            with self.assertRaises(CrawlBlocked):
                bounded_get(client, "https://example.org", max_bytes=16)

    def test_redirect_cannot_escape_host_allowlist(self):
        requests = []

        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(
                302, headers={"location": "https://other.example/"},
                request=request, extensions={"network_stream": _Peer(str(self.public_ip))},
            )

        with self._patch_dns(), self._client(handler) as client:
            with self.assertRaises(CrawlBlocked):
                bounded_get(client, "https://example.org", allowed_hosts={"example.org"})
        self.assertEqual(len(requests), 1)

    def test_connection_uses_pinned_ip_and_original_host_identity(self):
        observed = {}

        def handler(request):
            observed["url_host"] = request.url.host
            observed["host_header"] = request.headers.get("host")
            observed["sni"] = request.extensions.get("sni_hostname")
            return httpx.Response(
                200, content=b"ok", request=request,
                extensions={"network_stream": _Peer(str(self.public_ip))},
            )

        with self._patch_dns(), self._client(handler) as client:
            response = bounded_get(client, "https://example.org/path")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed["url_host"], str(self.public_ip))
        self.assertEqual(observed["host_header"], "example.org")
        self.assertEqual(observed["sni"], "example.org")

    def test_dns_peer_must_match_validated_address(self):
        def handler(request):
            return httpx.Response(
                200, content=b"ok", request=request,
                extensions={"network_stream": _Peer("1.1.1.1")},
            )

        with self._patch_dns(), self._client(handler) as client:
            with self.assertRaises(CrawlBlocked):
                bounded_get(client, "https://example.org")

if __name__ == "__main__":
    unittest.main()

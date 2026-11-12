import unittest

from sdi.normalise import canonical_page_url, domain_of, normalise_url


class NormaliseUrlTests(unittest.TestCase):
    def test_accepts_https_url(self):
        self.assertEqual(normalise_url("https://www.example.com/path"), "https://www.example.com/path")

    def test_adds_scheme_to_bare_domain(self):
        self.assertEqual(normalise_url("www.example.com"), "https://www.example.com")

    def test_rejects_phone_number_as_website(self):
        self.assertIsNone(normalise_url("+442039111115"))

    def test_rejects_tel_and_javascript_links(self):
        self.assertIsNone(normalise_url("tel:+442039111115"))
        self.assertIsNone(normalise_url("javascript:void(0)"))

    def test_rejects_malformed_and_nonstandard_ports(self):
        self.assertIsNone(normalise_url("https://example.com:abc"))
        self.assertIsNone(normalise_url("https://example.com:8080"))

    def test_canonical_page_url_removes_tracking_and_fragment(self):
        self.assertEqual(
            canonical_page_url("https://Example.com/path/?utm_source=news&b=2&a=1#section"),
            "https://example.com/path?a=1&b=2",
        )

    def test_canonical_page_url_preserves_functional_query(self):
        self.assertEqual(
            canonical_page_url("https://example.com/search?q=visa"),
            "https://example.com/search?q=visa",
        )

    def test_domain_of_handles_malformed_input(self):
        self.assertIsNone(domain_of("https://example.com:abc"))


if __name__ == "__main__":
    unittest.main()

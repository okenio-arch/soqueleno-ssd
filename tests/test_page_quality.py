import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from sdi.crawler import _indexable_page, crawl_organisation
from sdi.db import connect, initialise


class PageQualityTests(unittest.TestCase):
    def test_real_service_page_is_indexable(self):
        body = "Immigration solicitor services for skilled worker and family visa applications. " * 5
        self.assertTrue(_indexable_page("https://firm.example/services/immigration", "Immigration services", "", body))

    def test_search_and_archive_pages_are_rejected(self):
        body = "Immigration solicitor services for skilled worker and family visa applications. " * 5
        self.assertFalse(_indexable_page("https://firm.example/search?q=immigration", "Search", "", body))
        self.assertFalse(_indexable_page("https://firm.example/category/immigration", "Immigration", "", body))

    def test_thin_pages_are_rejected(self):
        self.assertFalse(_indexable_page("https://firm.example/contact", "Contact", "", "Call us today"))

    def test_changed_content_is_saved_as_a_new_page_version(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "sdi.sqlite3")
            initialise(db)
            with connect(db) as conn:
                conn.execute("""INSERT INTO organisations(canonical_name,normalized_name,website,domain)
                    VALUES('North Star Immigration','north star immigration','https://firm.example','firm.example')""")
                org_id = conn.execute("SELECT organisation_id FROM organisations").fetchone()[0]

            content = {"body": "Our immigration solicitors provide skilled worker visa and family visa advice. " * 8}
            def fake_get(client, url, **kwargs):
                html = f"<html><title>Immigration Solicitors</title><body>{content['body']}</body></html>"
                return httpx.Response(200, headers={"content-type": "text/html"},
                    text=html, request=httpx.Request("GET", url))

            with patch("sdi.crawler.validate_public_url", side_effect=lambda value: value), \
                 patch("sdi.crawler._robots_allows", return_value=True), \
                 patch("sdi.crawler._sitemaps", return_value=[]), \
                 patch("sdi.crawler.bounded_get", side_effect=fake_get):
                first = crawl_organisation(org_id, "https://firm.example/", db, max_pages=2, delay=0)
                content["body"] += " We also advise on asylum appeals and settlement applications."
                second = crawl_organisation(org_id, "https://firm.example/", db, max_pages=2, delay=0)

            with connect(db) as conn:
                versions = conn.execute("SELECT change_type,content_hash FROM page_versions ORDER BY version_id").fetchall()
            self.assertEqual(first["pages_crawled"], 1)
            self.assertEqual(second["pages_crawled"], 1)
            self.assertEqual(first["pages_indexed"], 1)
            self.assertEqual(second["pages_indexed"], 1)
            self.assertEqual(len(versions), 2)
            self.assertEqual([row["change_type"] for row in versions], ["new", "changed"])


if __name__ == "__main__":
    unittest.main()

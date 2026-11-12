import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from sdi.db import connect, initialise
from sdi.discovery import discover_from_seed


class SeedTraversalTests(unittest.TestCase):
    def test_internal_directory_pages_are_traversed_and_external_sites_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "sdi.sqlite3")
            initialise(db)
            with connect(db) as conn:
                conn.execute("""INSERT INTO sources(source_id,name,url,category,access_method,status,source_type,config_json)
                    VALUES('seed','Approved directory','https://directory.example','directory','manual','enabled',
                    'html_directory_seed','{"terms_reviewed":true,"crawl_allowed":true,"reuse_basis":"Terms permit this limited directory crawl."}')""")

            pages = {
                "https://directory.example/": '<html><title>Directory</title><body><a href="/directory?page=2">Next page</a></body></html>',
                "https://directory.example/directory?page=2": '<html><title>Immigration advisers</title><body><a href="https://firm.example/">North Star Immigration Solicitors</a></body></html>',
                "https://firm.example/": '<html><title>North Star Immigration Solicitors</title><body>' +
                    ('Our immigration solicitors provide skilled worker visa, family visa, asylum and settlement legal services. ' * 12) +
                    '</body></html>',
            }

            def fake_get(client, url, **kwargs):
                return httpx.Response(200, headers={"content-type": "text/html"},
                    text=pages[url], request=httpx.Request("GET", url))

            with patch("sdi.discovery.validate_public_url", side_effect=lambda value: value), \
                 patch("sdi.discovery._robots_allows", return_value=True), \
                 patch("sdi.discovery.bounded_get", side_effect=fake_get):
                result = discover_from_seed(
                    "https://directory.example/", db, source_id="seed",
                    max_candidates=10, max_seed_pages=5, delay=0,
                )

            self.assertEqual(result["pages_checked"], 3)
            self.assertEqual(result["candidates_accepted"], 1)
            with connect(db) as conn:
                candidate = conn.execute("SELECT status,discovery_type FROM discovered_urls WHERE domain='firm.example'").fetchone()
                org_count = conn.execute("SELECT count(*) FROM organisations WHERE domain='firm.example'").fetchone()[0]
            self.assertEqual(candidate["status"], "candidate_only")
            self.assertEqual(candidate["discovery_type"], "seed_candidate")
            self.assertEqual(org_count, 1)

    def test_seed_page_frontier_resumes_after_page_budget_is_reached(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "sdi.sqlite3")
            initialise(db)
            with connect(db) as conn:
                conn.execute("""INSERT INTO sources(source_id,name,url,category,access_method,status,source_type,config_json)
                    VALUES('seed','Approved directory','https://directory.example','directory','manual','enabled',
                    'html_directory_seed','{"terms_reviewed":true,"crawl_allowed":true,"reuse_basis":"Terms permit this limited directory crawl."}')""")

            pages = {
                "https://directory.example/": '<html><title>Directory</title><body><a href="/directory?page=2">Next page</a></body></html>',
                "https://directory.example/directory?page=2": '<html><title>Immigration advisers</title><body><a href="https://firm.example/">North Star Immigration Solicitors</a></body></html>',
                "https://firm.example/": '<html><title>North Star Immigration Solicitors</title><body>' +
                    ('Our immigration solicitors provide skilled worker visa, family visa, asylum and settlement legal services. ' * 12) +
                    '</body></html>',
            }
            calls = []
            def fake_get(client, url, **kwargs):
                calls.append(url)
                return httpx.Response(200, headers={"content-type": "text/html"},
                    text=pages[url], request=httpx.Request("GET", url))

            with patch("sdi.discovery.validate_public_url", side_effect=lambda value: value), \
                 patch("sdi.discovery._robots_allows", return_value=True), \
                 patch("sdi.discovery.bounded_get", side_effect=fake_get):
                first = discover_from_seed("https://directory.example/", db, source_id="seed",
                    max_candidates=10, max_seed_pages=1, delay=0)
                self.assertEqual(first["seed_pages_remaining"], 1)
                second = discover_from_seed("https://directory.example/", db, source_id="seed",
                    max_candidates=10, max_seed_pages=1, delay=0)

            self.assertEqual(second["candidates_accepted"], 1)
            self.assertEqual(second["seed_pages_remaining"], 0)
            self.assertEqual(calls.count("https://directory.example/"), 1)
            self.assertEqual(calls.count("https://directory.example/directory?page=2"), 1)


    def test_unapproved_seed_is_rejected_before_network_access(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "sdi.sqlite3")
            initialise(db)
            with connect(db) as conn:
                conn.execute("""INSERT INTO sources(source_id,name,url,category,access_method,status,source_type,config_json)
                    VALUES('seed','Unapproved directory','https://directory.example','directory','manual','blocked_pending_authorisation',
                    'html_directory_seed','{}')""")
            with patch("sdi.discovery.validate_public_url", side_effect=lambda value: value):
                with self.assertRaises(RuntimeError):
                    discover_from_seed("https://directory.example/", db, source_id="seed")


if __name__ == "__main__":
    unittest.main()

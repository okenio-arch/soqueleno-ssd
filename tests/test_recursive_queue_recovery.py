import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdi.db import connect, initialise
from sdi.recursive_discovery import discover_recursive_from_seed


class ApprovedSeedTraversalTests(unittest.TestCase):
    def test_directory_seed_traversal_does_not_promote_firm_websites_to_seeds(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "sdi.sqlite3")
            initialise(db)
            with connect(db) as conn:
                conn.execute("""INSERT INTO sources(source_id,name,url,category,access_method,status,source_type,config_json)
                    VALUES('seed','Seed','https://seed.example','directory','manual','enabled',
                    'html_directory_seed','{"terms_reviewed":true,"crawl_allowed":true,"reuse_basis":"Public directory terms permit this limited crawl."}')""")
                conn.execute("""INSERT INTO discovered_urls
                    (parent_url,discovered_url,domain,discovery_type,status,source_id,depth,attempts)
                    VALUES('https://seed.example','https://candidate.example','candidate.example',
                    'seed_candidate','candidate_only','seed',1,0)""")

            def fake_discovery(url, db_path, **kwargs):
                self.assertEqual(kwargs["source_id"], "seed")
                self.assertGreaterEqual(kwargs["max_seed_pages"], 1)
                return {"run_id": 1, "pages_checked": 4, "candidates_found": 3,
                        "candidates_accepted": 1, "candidates_rejected": 2,
                        "organisations_created": 1, "organisations_updated": 0,
                        "errors": []}

            with patch("sdi.recursive_discovery.validate_public_url", side_effect=lambda value: value), \
                 patch("sdi.recursive_discovery.discover_from_seed", side_effect=fake_discovery):
                result = discover_recursive_from_seed(
                    "https://seed.example", db, source_id="seed",
                    max_seed_depth=2, max_seed_pages=8, delay=0,
                )

            with connect(db) as conn:
                row = conn.execute("SELECT status FROM discovered_urls WHERE domain='candidate.example'").fetchone()
            self.assertEqual(row["status"], "candidate_only")
            self.assertEqual(result["domains_processed"], 1)
            self.assertEqual(result["pages_checked"], 4)
            self.assertEqual(result["candidates_accepted"], 1)


if __name__ == "__main__":
    unittest.main()

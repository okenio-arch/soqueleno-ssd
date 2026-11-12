import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdi.db import connect, initialise
from sdi.recursive_discovery import discover_recursive_from_seed


class RecursiveQueueRecoveryTests(unittest.TestCase):
    def test_processing_entry_is_recovered_and_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "sdi.sqlite3")
            initialise(db)
            with connect(db) as conn:
                conn.execute("""INSERT INTO sources(source_id,name,url,category,access_method,status)
                    VALUES('seed','Seed','https://seed.example','discovery','manual','enabled')""")
                conn.execute("""INSERT INTO discovered_urls
                    (parent_url,discovered_url,domain,discovery_type,status,source_id,depth,attempts)
                    VALUES('https://seed.example','https://candidate.example','candidate.example',
                    'recursive_seed','processing','seed',1,0)""")

            def fake_discovery(url, db_path, **kwargs):
                return {"run_id": None, "pages_checked": 1, "candidates_found": 0,
                        "candidates_accepted": 0, "candidates_rejected": 0,
                        "organisations_created": 0, "organisations_updated": 0,
                        "errors": []}

            with patch("sdi.recursive_discovery.validate_public_url", side_effect=lambda value: value), \
                 patch("sdi.recursive_discovery.discover_from_seed", side_effect=fake_discovery):
                result = discover_recursive_from_seed(
                    "https://seed.example", db, source_id="seed",
                    max_depth=2, max_domains=5, delay=0,
                )

            with connect(db) as conn:
                row = conn.execute(
                    "SELECT status,attempts,last_error FROM discovered_urls WHERE source_id='seed'"
                ).fetchone()
            self.assertEqual(row["status"], "processed")
            self.assertEqual(row["attempts"], 1)
            self.assertIsNone(row["last_error"])
            self.assertEqual(result["domains_processed"], 2)

if __name__ == "__main__":
    unittest.main()

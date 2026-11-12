import tempfile
import unittest
from pathlib import Path

from sdi.db import SCHEMA, connect, initialise


class SchemaUpgradeTests(unittest.TestCase):
    def test_legacy_schema_receives_new_provenance_and_queue_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "legacy.sqlite3")
            with connect(db) as conn:
                conn.executescript(SCHEMA)
            initialise(db)
            with connect(db) as conn:
                expected = {
                    "evidence": {"confidence", "source_authority"},
                    "discovered_urls": {"source_id", "depth", "attempts", "last_attempt_at", "last_error"},
                    "crawled_pages": {"duplicate_of_url", "is_indexable"},\n                    "page_versions": {"page_id", "content_hash", "change_type", "captured_at"},
                }
                for table, required in expected.items():
                    actual = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                    self.assertTrue(required.issubset(actual), f"{table} missing {required - actual}")
                version = conn.execute("SELECT value FROM sdi_meta WHERE key='search_index_version'").fetchone()[0]
                self.assertEqual(version, "2")

if __name__ == "__main__":
    unittest.main()

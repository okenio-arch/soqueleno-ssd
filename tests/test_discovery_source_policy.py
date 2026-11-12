import tempfile
import unittest
from pathlib import Path

from sdi.db import initialise, connect
from sdi.discovery import _blocked_source_reason


class DiscoverySourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "sdi.sqlite3")
        initialise(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_blocked_registry_domain_is_rejected_before_fetch(self):
        with connect(self.db) as conn:
            conn.execute(
                """INSERT INTO sources(source_id,name,url,category,access_method,status,source_type)
                VALUES('law_society','Law Society','https://solicitors.lawsociety.org.uk/',
                'directory','terms_review','blocked_pending_authorisation','manual_or_authorisation_pending')"""
            )
        reason = _blocked_source_reason(self.db, "solicitors.lawsociety.org.uk")
        self.assertIsNotNone(reason)
        self.assertIn("blocked_pending_authorisation", reason)

    def test_subdomain_of_blocked_source_is_rejected(self):
        with connect(self.db) as conn:
            conn.execute(
                """INSERT INTO sources(source_id,name,url,category,access_method,status,source_type)
                VALUES('sra','SRA','https://www.sra.org.uk/datasharing/',
                'regulator','manual','blocked_pending_authorisation','manual_or_authorisation_pending')"""
            )
        self.assertIsNotNone(_blocked_source_reason(self.db, "api.sra.org.uk"))

    def test_unlisted_domain_is_not_blocked(self):
        self.assertIsNone(_blocked_source_reason(self.db, "independent-immigration-firm.co.uk"))


if __name__ == "__main__":
    unittest.main()

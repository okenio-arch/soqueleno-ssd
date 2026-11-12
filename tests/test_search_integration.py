import tempfile
import unittest
from pathlib import Path

from sdi.db import initialise, connect
from sdi.search import search_everything, search_organisations


class SearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "sdi.sqlite3")
        initialise(self.db)
        with connect(self.db) as conn:
            conn.execute("""INSERT INTO sources(source_id,name,url,category,access_method,status)
                VALUES('test','Test source','https://example.org','test','manual','enabled')""")
            conn.execute("""INSERT INTO organisations(canonical_name,normalized_name,website,domain,immigration_services)
                VALUES('North Star Immigration','north star immigration','https://northstar.example','northstar.example','skilled worker visa family visa')""")
            org_id = conn.execute("SELECT organisation_id FROM organisations").fetchone()[0]
            conn.execute("""INSERT INTO crawled_pages(organisation_id,source_id,url,http_status,title,description,text_content,crawl_status,is_indexable)
                VALUES(?,?,?,?,?,?,?,?,1)""",
                (org_id, 'organisation_websites', 'https://northstar.example/services',
                 200, 'Immigration services', 'UK visa advice',
                 'Our team advises on skilled worker visa applications and family immigration.', 'success'))

    def tearDown(self):
        self.temp.cleanup()

    def test_organisation_search_returns_match(self):
        rows = search_organisations(self.db, "skilled worker")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["canonical_name"], "North Star Immigration")

    def test_page_search_returns_query_context(self):
        result = search_everything(self.db, "skilled worker")
        self.assertEqual(len(result["pages"]), 1)
        self.assertIn("<mark>", result["pages"][0]["snippet"])

    def test_non_indexable_pages_are_excluded(self):
        with connect(self.db) as conn:
            conn.execute("UPDATE crawled_pages SET is_indexable=0")
        result = search_everything(self.db, "skilled worker")
        self.assertEqual(result["pages"], [])

if __name__ == "__main__":
    unittest.main()

import unittest

from sdi.crawler import _indexable_page


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


if __name__ == "__main__":
    unittest.main()

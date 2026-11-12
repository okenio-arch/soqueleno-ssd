import unittest
from sdi.datasets import _merge_categories

class CategoryMergeTests(unittest.TestCase):
    def test_deduplicates_case_insensitively(self):
        self.assertEqual(
            _merge_categories("Immigration; Asylum", "immigration; Family Visa"),
            "Immigration; Asylum; Family Visa",
        )

    def test_handles_empty_values(self):
        self.assertIsNone(_merge_categories(None, ""))
        self.assertEqual(_merge_categories(None, "Immigration"), "Immigration")

if __name__ == "__main__":
    unittest.main()

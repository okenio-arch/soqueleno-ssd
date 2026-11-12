import unittest
from sdi.search import _fts_query

class FtsQueryTests(unittest.TestCase):
    def test_prefix_terms_are_quoted(self):
        self.assertEqual(_fts_query("immigration adviser"), '"immigration"* AND "adviser"*')
    def test_operator_input_is_treated_as_words(self):
        query = _fts_query('immigration OR "visa" AND (worker*)')
        self.assertIn('"immigration"*', query)
        self.assertIn('"visa"*', query)
        self.assertIn('"worker"*', query)
    def test_empty_symbols_rejected(self):
        with self.assertRaises(ValueError):
            _fts_query(" *** !!! ")
    def test_query_term_limit(self):
        self.assertEqual(len(_fts_query("one two three four five six seven eight nine ten eleven twelve thirteen").split(" AND ")), 12)

if __name__ == "__main__":
    unittest.main()

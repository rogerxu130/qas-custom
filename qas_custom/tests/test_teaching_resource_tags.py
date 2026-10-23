from unittest import TestCase
from qas_custom.services.teaching_resource_rules import normalize_tag


class TagNormalizationTests(TestCase):
    def test_case_spacing_and_accidental_delimiters_share_one_identity(self):
        for value in ['Sketching', 'SKETCHING', ' sketching, ', '，Sketching，']:
            self.assertEqual(normalize_tag(value), 'sketching')
        self.assertEqual(normalize_tag('  Animal   Sketching  '), 'animal sketching')

    def test_invalid_names_cannot_enter_the_library(self):
        for value in ['', ',，', 'sketching,animals', '写实，初级', 'x' * 101, None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_tag(value)

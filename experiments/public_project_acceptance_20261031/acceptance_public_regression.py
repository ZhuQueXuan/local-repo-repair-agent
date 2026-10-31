import unittest
import inflection

class PublicRegressionTests(unittest.TestCase):
    def test_accented_second_word(self):
        self.assertEqual(inflection.titleize('Ana Índia'), 'Ana Índia')
    def test_lowercase_accented_second_word(self):
        self.assertEqual(inflection.titleize('ana índia'), 'Ana Índia')
    def test_accented_first_word(self):
        self.assertEqual(inflection.titleize('Índia'), 'Índia')
    def test_plain_title(self):
        self.assertEqual(inflection.titleize('Ana India'), 'Ana India')
    def test_passerby_plural(self):
        self.assertEqual(inflection.pluralize('passerby'), 'passersby')
    def test_passerby_singular(self):
        self.assertEqual(inflection.singularize('passersby'), 'passerby')
    def test_regular_compatibility(self):
        self.assertEqual(inflection.pluralize('cat'), 'cats')
        self.assertEqual(inflection.singularize('cats'), 'cat')
    def test_name_transform_compatibility(self):
        self.assertEqual(inflection.camelize('active_record'), 'ActiveRecord')
        self.assertEqual(inflection.underscore('ActiveRecord'), 'active_record')

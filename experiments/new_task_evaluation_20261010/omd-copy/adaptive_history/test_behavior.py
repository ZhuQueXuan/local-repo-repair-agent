import copy
import pickle
import unittest
from boltons.dictutils import OrderedMultiDict as OMD


class CopyBehavior(unittest.TestCase):
    def test_shallow_preserves_all_pairs(self):
        for pairs in [[], [('a', 1)], [('a', 1), ('b', 2), ('a', 3), ('a', 3)]]:
            with self.subTest(pairs=pairs):
                value = OMD(pairs)
                copied = copy.copy(value)
                self.assertIsNot(copied, value)
                self.assertEqual(list(copied.items(multi=True)), pairs)
                self.assertEqual(list(value.items(multi=True)), pairs)

    def test_deep_preserves_all_pairs(self):
        pairs = [('a', 1), ('b', 2), ('a', 3)]
        self.assertEqual(list(copy.deepcopy(OMD(pairs)).items(multi=True)), pairs)

    def test_shallow_shares_values(self):
        shared = [1]
        original = OMD([('a', shared), ('a', [2])])
        copied = copy.copy(original)
        self.assertIs(copied.getlist('a')[0], shared)
        copied.getlist('a')[0].append(3)
        self.assertEqual(original.getlist('a')[0], [1, 3])

    def test_copy_container_is_independent(self):
        original = OMD([('a', 1), ('b', 2), ('a', 3)])
        for copier in (copy.copy, copy.deepcopy):
            with self.subTest(copier=copier.__name__):
                copied = copier(original)
                copied.add('a', 4)
                copied.pop('b')
                self.assertEqual(original.getlist('a'), [1, 3])
                self.assertEqual(original['b'], 2)
                self.assertEqual(copied.getlist('a'), [1, 3, 4])

    def test_deep_values_independent_and_alias_preserved(self):
        shared = {'nested': [1]}
        original = OMD([('a', shared), ('b', shared), ('a', {'nested': [2]})])
        copied = copy.deepcopy(original)
        first = copied.getlist('a')[0]
        self.assertIs(first, copied['b'])
        self.assertIsNot(first, shared)
        first['nested'].append(9)
        self.assertEqual(shared['nested'], [1])
        self.assertEqual(copied.getlist('a')[1], {'nested': [2]})

    def test_deep_self_reference(self):
        original = OMD([('a', 1), ('a', 2)])
        original.add('self', original)
        copied = copy.deepcopy(original)
        self.assertIs(copied['self'], copied)
        self.assertEqual(copied.getlist('a'), [1, 2])

    def test_subclass_preserved(self):
        class Child(OMD):
            pass
        for copier in (copy.copy, copy.deepcopy):
            with self.subTest(copier=copier.__name__):
                copied = copier(Child([('a', 1), ('a', 2)]))
                self.assertIs(type(copied), Child)
                self.assertEqual(copied.getlist('a'), [1, 2])

    def test_existing_pickle_protocols_preserved(self):
        pairs = [('a', 1), ('b', [2]), ('a', 3)]
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.subTest(protocol=protocol):
                restored = pickle.loads(pickle.dumps(OMD(pairs), protocol=protocol))
                self.assertEqual(list(restored.items(multi=True)), pairs)

    def test_existing_method_copy_preserved(self):
        pairs = [('a', 1), ('b', 2), ('a', 3)]
        self.assertEqual(list(OMD(pairs).copy().items(multi=True)), pairs)

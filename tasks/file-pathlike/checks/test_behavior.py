import os
from pathlib import Path
import tempfile
import unittest
from boltons import fileutils


class CustomPath:
    def __init__(self, path):
        self.path = path
    def __fspath__(self):
        return str(self.path)


class PathBehavior(unittest.TestCase):
    def setUp(self):
        # Files stay in the disposable checkout's temp; no recursive cleanup.
        self.root = Path(tempfile.mkdtemp(prefix='path_checks_', dir=Path('temp'))).resolve()

    def assert_saved(self, destination, **kwargs):
        with fileutils.atomic_save(destination, **kwargs) as stream:
            stream.write(b'first\nsecond')
        path = Path(os.fspath(destination))
        self.assertEqual(path.read_bytes(), b'first\nsecond')
        self.assertFalse(Path(str(path) + '.part').exists())

    def test_atomic_save_path(self):
        self.assert_saved(self.root / 'path.txt')

    def test_atomic_saver_custom_pathlike(self):
        destination = self.root / 'custom.txt'
        with fileutils.AtomicSaver(CustomPath(destination)) as stream:
            stream.write(b'custom')
        self.assertEqual(destination.read_bytes(), b'custom')

    def test_atomic_save_unicode_spaces(self):
        self.assert_saved(self.root / '测试 file.txt')

    def test_atomic_save_relative_path(self):
        relative = (self.root / 'relative.txt').relative_to(Path.cwd())
        self.assert_saved(relative)

    def test_atomic_save_string_regression(self):
        self.assert_saved(str(self.root / 'old.txt'))

    def test_atomic_save_explicit_part_name(self):
        self.assert_saved(self.root / 'named.txt', part_file='chosen.part')
        self.assertFalse((self.root / 'chosen.part').exists())

    def test_atomic_save_overwrite(self):
        path = self.root / 'overwrite.txt'
        path.write_bytes(b'old')
        self.assert_saved(path)

    def test_iter_find_files_path(self):
        (self.root / 'yes.txt').write_text('yes')
        (self.root / 'no.bin').write_text('no')
        found = list(fileutils.iter_find_files(self.root, patterns=['*.txt']))
        self.assertEqual([Path(p).name for p in found], ['yes.txt'])

    def test_iter_find_files_custom_pathlike(self):
        (self.root / 'found.txt').write_text('yes')
        found = list(fileutils.iter_find_files(CustomPath(self.root), patterns=['*.txt']))
        self.assertEqual([Path(p).name for p in found], ['found.txt'])

    def test_iter_find_files_string_regression(self):
        (self.root / 'keep.txt').write_text('yes')
        (self.root / 'ignored.txt').write_text('no')
        found = list(fileutils.iter_find_files(str(self.root), patterns=['*.txt'], ignored=['ignored*']))
        self.assertEqual([Path(p).name for p in found], ['keep.txt'])

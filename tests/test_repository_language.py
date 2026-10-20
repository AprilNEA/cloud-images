"""Keep repository text and file names free of Han characters."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
RANGES = (
    (0x2E80, 0x2EFF), (0x2F00, 0x2FDF), (0x31C0, 0x31EF),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x20000, 0x323AF),
)


def contains_han(text):
    return any(start <= ord(character) <= end
               for character in text for start, end in RANGES)


class RepositoryLanguageTests(unittest.TestCase):
    def test_tracked_paths_and_text_have_no_han_characters(self):
        paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        for name in filter(None, paths):
            with self.subTest(path=name):
                self.assertFalse(contains_han(name), 'File name contains a Han character')
                path = ROOT / name
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding='utf-8')
                except UnicodeDecodeError:
                    continue
                self.assertFalse(contains_han(text), 'File content contains a Han character')

    def test_detection_covers_common_and_extension_characters(self):
        self.assertTrue(contains_han(chr(0x4E00)))
        self.assertTrue(contains_han(chr(0x20000)))
        self.assertFalse(contains_han('English repository text.'))


if __name__ == '__main__':
    unittest.main()

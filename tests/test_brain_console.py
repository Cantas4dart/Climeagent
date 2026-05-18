import io
import unittest
from unittest.mock import patch

from brain.console import safe_print


class FailingStdout:
    encoding = "cp1252"

    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, value):
        raise UnicodeEncodeError("charmap", value, 0, 1, "character maps to <undefined>")

    def flush(self):
        return None


class SafePrintTests(unittest.TestCase):
    def test_safe_print_falls_back_when_stdout_cannot_encode_unicode(self):
        fake_stdout = FailingStdout()

        with patch("sys.stdout", fake_stdout):
            safe_print("Esenboğa")

        self.assertIn(b"Esenbo\\u011fa", fake_stdout.buffer.getvalue())


if __name__ == "__main__":
    unittest.main()

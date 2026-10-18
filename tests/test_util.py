from __future__ import print_function

import os
import tempfile
import unittest

from proctor.util import (
    human_bytes, is_within, parse_duration, parse_map, safe_archive_name, safe_join,
    sha256_file, atomic_write,
)


class UtilTests(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(1024), "1.0 KiB")
        self.assertEqual(human_bytes(1536), "1.5 KiB")

    def test_parse_duration(self):
        self.assertEqual(parse_duration("30s"), 30)
        self.assertEqual(parse_duration("15m"), 900)
        self.assertEqual(parse_duration("7d"), 604800)

    def test_is_within(self):
        self.assertTrue(is_within("/var/lib/prosody/x", "/var/lib/prosody"))
        self.assertTrue(is_within("/var/lib/prosody", "/var/lib/prosody"))
        self.assertFalse(is_within("/var/lib/prosodyx", "/var/lib/prosody"))
        self.assertFalse(is_within("/etc", "/var/lib/prosody"))

    def test_safe_archive_name(self):
        self.assertEqual(safe_archive_name("/etc/prosody/prosody.cfg.lua"),
                         "etc/prosody/prosody.cfg.lua")
        self.assertEqual(safe_archive_name("/var/lib/prosody/../etc"), "var/lib/etc")

    def test_safe_join_rejects_traversal(self):
        root = tempfile.mkdtemp()
        self.assertTrue(safe_join(root, "etc/prosody").startswith(root))
        with self.assertRaises(Exception):
            safe_join(root, "../etc/passwd")
        with self.assertRaises(Exception):
            safe_join(root, "/etc/passwd")

    def test_parse_map(self):
        result = parse_map(["a=b", "c=d"])
        self.assertEqual(len(result), 2)
        with self.assertRaises(Exception):
            parse_map(["/=tmp"])
        with self.assertRaises(Exception):
            parse_map(["=tmp"])
        with self.assertRaises(Exception):
            parse_map(["missing-equals"])

    def test_atomic_write_and_hash(self):
        root = tempfile.mkdtemp()
        path = os.path.join(root, "file")
        atomic_write(path, b"hello", mode=0o600)
        self.assertEqual(sha256_file(path), (
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824", 5))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_sha256_file_detects_changes(self):
        root = tempfile.mkdtemp()
        path = os.path.join(root, "file")
        atomic_write(path, b"one")
        first = sha256_file(path)[0]
        atomic_write(path, b"two")
        self.assertNotEqual(first, sha256_file(path)[0])


if __name__ == "__main__":
    unittest.main()

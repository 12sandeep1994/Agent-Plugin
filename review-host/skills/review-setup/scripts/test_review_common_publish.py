"""Artifacts publish once, also on sandbox filesystems that refuse hard links."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review_common import ReviewError, write_new_text


def refuse_hard_links(source, target):
    raise PermissionError(1, "Operation not permitted", str(source), None, str(target))


class PublishTests(unittest.TestCase):
    def directory(self) -> Path:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        return Path(scratch.name)

    def test_without_hard_links_the_text_is_published_and_no_temporary_is_left(self):
        directory = self.directory()
        with mock.patch("review_common.os.link", side_effect=refuse_hard_links):
            write_new_text(directory / "owner.json", "{}\n")
        self.assertEqual((directory / "owner.json").read_text(encoding="utf-8"), "{}\n")
        self.assertEqual(sorted(os.listdir(directory)), ["owner.json"])

    def test_without_hard_links_an_existing_artifact_is_never_overwritten(self):
        directory = self.directory()
        (directory / "owner.json").write_text("first\n", encoding="utf-8")
        with mock.patch("review_common.os.link", side_effect=refuse_hard_links):
            with self.assertRaises(ReviewError):
                write_new_text(directory / "owner.json", "second\n")
        self.assertEqual((directory / "owner.json").read_text(encoding="utf-8"), "first\n")
        self.assertEqual(sorted(os.listdir(directory)), ["owner.json"])

    def test_with_hard_links_an_existing_artifact_is_never_overwritten(self):
        directory = self.directory()
        write_new_text(directory / "owner.json", "first\n")
        with self.assertRaises(ReviewError):
            write_new_text(directory / "owner.json", "second\n")
        self.assertEqual((directory / "owner.json").read_text(encoding="utf-8"), "first\n")
        self.assertEqual(sorted(os.listdir(directory)), ["owner.json"])


if __name__ == "__main__":
    unittest.main()

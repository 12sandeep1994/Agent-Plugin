"""The process census works in a gVisor sandbox, whose procfs has no NSpid line, and still fails closed."""
import os
import sys
import unittest
from pathlib import PosixPath
from unittest import mock

import review_common
from review_common import CleanupNotProvenError, _process_census


def procfs(status_edit):
    """A Path whose /proc/self/status text passes through status_edit, as another kernel would report it."""
    class EditedPath(PosixPath):
        def read_text(self, *args, **kwargs):
            text = super().read_text(*args, **kwargs)
            return status_edit(text) if str(self) == "/proc/self/status" else text
    return mock.patch.object(review_common, "Path", EditedPath)


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux procfs")
class CensusTests(unittest.TestCase):
    def test_a_status_without_nspid_still_gives_a_census_that_includes_this_process(self):
        without_nspid = lambda text: "\n".join(l for l in text.splitlines() if not l.startswith("NSpid:"))
        with procfs(without_nspid):
            rows = _process_census()
        self.assertIn(os.getpid(), [row["tgid"] for row in rows])

    def test_an_nspid_from_a_nested_namespace_still_fails_closed(self):
        nested = lambda text: "\n".join(
            f"NSpid:\t{os.getpid() + 1000}\t{os.getpid()}" if l.startswith("NSpid:") else l
            for l in text.splitlines()
        )
        with procfs(nested):
            with self.assertRaises(CleanupNotProvenError):
                _process_census()


if __name__ == "__main__":
    unittest.main()

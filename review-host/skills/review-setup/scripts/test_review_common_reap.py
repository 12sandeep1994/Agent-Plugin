"""Exited orphans of an owned group are proven gone even where procfs cannot show their zombies (gVisor)."""
import ctypes
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import review_common
from review_common import _group_confirmed_empty, _reap_group_zombies


def spawn_group(script):
    """Start sh in a fresh session; return its Popen and process group id."""
    process = subprocess.Popen(["sh", "-c", script], start_new_session=True)
    return process, os.getpgid(process.pid)


def invisible_zombies():
    """gVisor lists no task directory for a zombie, so the census omits it."""
    return mock.patch.object(review_common, "_process_census", lambda: [])


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux process groups")
class ReapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # As run_owned does before every launch: orphans of an owned group come to this process.
        review_common._become_subreaper()
        assert ctypes.CDLL(None).prctl(37, ctypes.byref(flag := ctypes.c_int()), 0, 0, 0) == 0 and flag.value == 1

    def test_an_exited_orphan_that_the_census_cannot_see_is_reaped_and_the_group_proven_empty(self):
        process, pgid = spawn_group("sleep 0.1 & exit 0")
        process.wait()
        time.sleep(0.4)  # The orphan has exited and waits for its adopted parent.
        with invisible_zombies():
            self.assertTrue(_group_confirmed_empty(pgid, time.monotonic() + 1))

    def test_a_live_orphan_is_never_reaped_and_the_group_stays_unproven(self):
        process, pgid = spawn_group("sleep 30 & exit 0")
        process.wait()
        try:
            with invisible_zombies():
                self.assertFalse(_group_confirmed_empty(pgid, time.monotonic() + 0.3))
        finally:
            os.killpg(pgid, 9)
            time.sleep(0.2)
            _reap_group_zombies(pgid)

    def test_an_unreaped_leader_keeps_its_exit_status_for_popen(self):
        process, pgid = spawn_group("exit 7")
        time.sleep(0.3)  # The leader has exited, but Popen has not reaped it yet.
        _reap_group_zombies(pgid)
        self.assertEqual(process.wait(timeout=5), 7)


if __name__ == "__main__":
    unittest.main()

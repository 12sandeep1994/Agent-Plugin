"""Whole-worktree Git calls in prepare and commit get the longer limit; a large tree on a slow mount outlasted 300 s."""
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import review_commit as commit
import review_git_prepare as prepare
from review_common import WORKTREE_GIT_TIMEOUT


class WorktreeGitTimeoutTests(unittest.TestCase):
    def test_nested_reset_clean_and_checkout_get_the_worktree_limit(self):
        with patch.object(prepare, "worktree_rows", return_value=[]), \
                patch.object(prepare, "_find_worktree_row", return_value=object()), \
                patch.object(prepare, "_worktree_functional", return_value=True), \
                patch.object(prepare, "_clear_operation_markers"), \
                patch.object(prepare, "run_git") as run_git:
            prepare._prepare_nested(MagicMock(read_only=False), MagicMock(), Path("/nested"), "a" * 40)
        self.assertEqual(
            [call.args[1][0] for call in run_git.call_args_list],
            ["reset", "clean", "checkout", "reset", "clean"],
        )
        for call in run_git.call_args_list:
            self.assertEqual(call.kwargs.get("timeout"), prepare._WORKTREE_GIT_TIMEOUT)
        self.assertGreater(prepare._WORKTREE_GIT_TIMEOUT, 300)


class CommitReadTimeoutTests(unittest.TestCase):
    def test_source_fingerprint_whole_tree_reads_get_the_worktree_limit(self):
        def fake_read(repo, args, check=True, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch.object(commit, "run_git_read", side_effect=fake_read) as run_git_read, \
                patch.object(commit, "current_ref", return_value=None):
            commit.source_fingerprint(Path("/source"))
        commands = [call.args[1][:2] for call in run_git_read.call_args_list]
        for expected in (["diff", "--no-ext-diff"], ["ls-files", "--others"], ["status", "--porcelain=v1"]):
            self.assertIn(expected, commands)
        for call in run_git_read.call_args_list:
            self.assertEqual(call.kwargs.get("timeout"), WORKTREE_GIT_TIMEOUT)
        self.assertGreater(WORKTREE_GIT_TIMEOUT, 300)


class WholeTreeScanTimeoutTests(unittest.TestCase):
    def test_status_and_ignored_scans_get_the_worktree_limit(self):
        # Prepare's postconditions and reviewd's slot admission call these on the outer worktree.
        import review_common as common

        for name in ("run_git", "run_git_read"):
            with patch.object(common, name, return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as runner:
                read_only = name == "run_git_read"
                common.git_status(Path("/source"), read_only=read_only)
                common.git_ignored_paths(Path("/source"), read_only=read_only)
            self.assertEqual(runner.call_count, 2)
            for call in runner.call_args_list:
                self.assertEqual(call.kwargs.get("timeout"), WORKTREE_GIT_TIMEOUT)


if __name__ == "__main__":
    unittest.main()

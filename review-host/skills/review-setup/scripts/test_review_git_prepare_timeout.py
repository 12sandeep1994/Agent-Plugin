"""Whole-worktree Git rewrites in prepare get the longer limit; a large tree on a slow mount outlasted 300 s."""
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import review_git_prepare as prepare


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


if __name__ == "__main__":
    unittest.main()

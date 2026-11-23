import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from review_common import shallow_fetch_args


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


class ShallowFetchArgsTests(unittest.TestCase):
    def test_shallow_updating_fetches_compare_only_size_and_mtime(self):
        args = shallow_fetch_args("fetch", "--update-shallow")

        self.assertEqual(
            ["-c", "core.checkStat=minimal", "fetch", "--update-shallow"], args
        )

    def test_git_deepens_a_shallow_store_with_the_prefixed_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            origin = Path(temp, "origin")
            origin.mkdir()
            self.assertEqual(0, _git(origin, "init", "-q").returncode)
            for index in range(3):
                result = _git(origin, "commit", "-q", "--allow-empty", "-m", str(index))
                self.assertEqual(0, result.returncode, result.stderr)
            store = Path(temp, "store.git")
            clone = _git(
                Path(temp), "clone", "-q", "--bare", "--depth=1",
                origin.as_uri(), str(store),
            )
            self.assertEqual(0, clone.returncode, clone.stderr)

            deepened = _git(
                store,
                *shallow_fetch_args(
                    "-c", "protocol.file.allow=always", "fetch", "--no-tags",
                    "--update-shallow", "--deepen=1", "--", origin.as_uri(), "HEAD",
                ),
            )

            self.assertEqual(0, deepened.returncode, deepened.stderr)
            count = _git(store, "rev-list", "--count", "FETCH_HEAD").stdout.strip()
            self.assertEqual("2", count)


if __name__ == "__main__":
    unittest.main()

# review-host

Host kit for a pull-request review workspace that runs inside a sandbox. It prepares the
PR's real merge result in an isolated worktree slot, gathers provider context, merges the
finished review into the local review repository, and calls Azure DevOps or GitHub REST
through the sandbox egress proxy without ever holding a credential.

## Skills

| Skill | Purpose | Origin |
|---|---|---|
| `review-setup` | Slot pool, PR merge preparation, reset/archive, provider context, review commit and merge scripts (`scripts/`). | Verbatim copy |
| `repo-setup` | Prepare the review repository's `repos/` submodules for cross-repo search. | Verbatim copy |
| `merge-review` | Drive `review-setup/scripts/merge_review.py` to merge a finished review locally. | Verbatim copy |
| `review-host-rules` | Operating rules for a review workspace: slot discipline, verification, posting. | Written for this plugin |
| `sandbox-http` | `scripts/sandbox_fetch.py`: one HTTPS request via `curl` through the egress proxy, JSON result, fixed exit codes. | Written for this plugin |

## Source

The three verbatim skills are generated from AgentV2 (LmDotnetTools). Do not edit them
here; change them upstream and re-sync.

<!-- sync:source-start -->
Source: AgentV2 `samples/CodeReviewDaemon.Sample/review-host-kit/skills/` at commit `7c8a7f0cdb6488f4c8761ce2d22312c5ceb505b2`.
<!-- sync:source-end -->

Re-sync from an AgentV2 checkout (copies committed content of `--ref`, default `HEAD`,
removes stale files, updates the commit above; running it again changes nothing):

```
python scripts/sync_from_agentv2.py /path/to/AgentV2 [--ref origin/main] [--check]
```

`--check` reports drift without writing and exits 1 when out of sync.

## Requirements

- Python 3.10+ (standard library only).
- Git 2.48+ (worktree `--relative-paths`).
- Linux: the kit uses `fcntl` for lifecycle locks, so it does not run on Windows.
- `curl` for `sandbox-http`.
- Credentials come from the sandbox egress proxy. Nothing in this plugin stores, sends or
  asks for a token.

## Using it from a plugin mount

- The plugin directory is read-only. Set `PYTHONDONTWRITEBYTECODE=1` and write review
  output only under the review workspace, never under the plugin.
- Keep `skills/review-setup/scripts/` intact: every script imports `review_common.py` as a
  sibling, and `repo-setup/scripts/repo_setup.py` imports it from
  `../../review-setup/scripts/`.
- The verbatim scripts infer the review root from their own location
  (`parents[4]` of `review_common.py`), which only works in the original
  `<root>/.claude/skills/...` layout. From a plugin mount, pass `--root <review root>` to
  every script. Two code paths still infer the root: `review_ado.py` when `--org` is not
  given, and `review_reset.py` logging when `--root` is absent. Pass `--org` and `--root`.
- The verbatim SKILL.md files show `/workspace/.claude/skills/...` paths. Read them as
  paths inside this plugin.

## reviewd integration (not done yet)

AgentV2's reviewd (`samples/Reviewd.Sample`) currently expects the scripts at
`/workspace/.claude/skills/review-setup/scripts`
(`ReviewRunnerSandbox.py:177`, `MergeSandbox.py:105`, `JudgeSandbox.py:126`), and its
prompts point at the old gateway helper
`/marketplaces/sandbox-plugins/sandbox-auth/scripts/sandbox-auth-fetch.py`. To use this
plugin, reviewd needs a setting for the scripts path and the `sandbox_fetch.py` path.

## Tests

```
python -m unittest discover -s tests -v
```

`tests/test_sandbox_fetch.py` uses a fake `curl`; no network. The kit's own tests are
`skills/review-setup/scripts/test_*.py` (Linux only).

---
name: review-host-rules
description: Operating rules for a pull-request review workspace that uses the review-setup, repo-setup and merge-review skills. Load before preparing, reviewing, resetting or merging a review slot, or before posting anything to a PR.
---

# Review workspace operating rules

A review workspace is an outer Git repository that reviews product repositories. Product
repositories are submodules under `repos/`. Reviews run in isolated, disposable worktree
slots under `.worktrees/`. Durable output goes to `PRs/` (per-PR evidence and final review)
and `KnowledgeBase/` (lessons that hold beyond one PR). Local operational state lives under
`.git/review-setup/` and `.worktrees/`; it is not disposable build output.

## Paths in this plugin

- Resolve `PLUGIN_ROOT` from the Skill tool header (two levels above a skill's directory).
  Scripts are under `PLUGIN_ROOT/skills/review-setup/scripts/`.
- The plugin mount is read-only. Never write under it.
- These scripts infer the review root from their own location only in the original
  `.claude/skills` layout. From this plugin, pass `--root <review workspace root>` (for
  example `--root /workspace`) to every script that accepts it; for `review_pool_admin.py`
  put it before the subcommand. If a script says it expected the canonical root, `--root`
  is missing.
- Skill text that shows `/workspace/.claude/skills/review-setup/scripts/...` means
  `PLUGIN_ROOT/skills/review-setup/scripts/...` here.

## Review principles

1. Review the PR's actual merge result, not an approximation of its source branch.
2. Prefer source-grounded findings with exact file and line references.
3. Report only actionable defects or material risks introduced by the PR.
4. Deduplicate across specialist reviewers and existing PR discussion.
5. Separate verified facts from assumptions and unavailable provider data.
6. Keep enough evidence for another reviewer to reproduce each conclusion.
7. Do not edit product source during a review unless explicitly asked.
8. Never push product or app branches, complete a PR, or post comments unless the user
   explicitly requests and approves that action.

## Slot discipline

- Use the `review-setup` skill for every prepare, inspect, reset, archive or merge. Read its
  SKILL.md in full before running a command.
- Inspect the pool first (read-only): `review_pool_admin.py status`. A directory that exists
  is not a ready slot.
- Setup is destructive to the selected slot. Pick the slot explicitly and verify it first.
- Slots are repository-qualified (`<Repo>-<n>`). The same number in another repository is a
  different slot.
- Different slots may run concurrently. Never run two operations on the same slot.
- Never `rm -rf`, `git clean`, `git reset --hard`, prune, recreate or repoint a managed slot.
- Never hand-edit `.git/review-setup/` manifests, locks, journals or source-store refs.
- Never bypass the scripts' locks with direct Git commands.
- On topology drift, a foreign directory, an interrupted operation or an ownership conflict:
  stop and report. Do not improvise recovery.
- Prefer archive over discard when resetting. Discard needs the exact confirmation key.

## Verify before reviewing

After setup, confirm:

1. The prepared-slot manifest names the requested repository, slot, PR and merge SHA.
2. The nested source worktree is detached at exactly that merge SHA and is clean,
   including ignored files.
3. The outer worktree is at its expected review-store baseline.
4. No merge, rebase, cherry-pick, revert, bisect or live writer remains in the slot.
5. Provider context matches the same PR iteration and merge SHA when available.

Setup exit codes: `0` ready; `1` failed, do not use the slot; `2` Git ready but context has
warnings; `3` the PR moved, re-resolve and re-prepare.

## Doing the review

- Inspect source only in `.worktrees/<Repo>-<n>/repos/<Repo>`.
- Search committed code locally with `git grep`, `rg` and exact-commit `git show`. Keep
  searches focused on the change and its callers.
- If provider metadata is unavailable but the verified prepared merge commit exists,
  continue from local evidence and state what could not be verified.
- Specialist results are inputs to one lead review. The lead validates every finding
  against the diff, removes duplicates and out-of-scope items, accounts for existing PR
  comments, sets severity by impact, and gives a concrete fix and closure condition.
- Store artifacts under `PRs/<repo-slug>-<pr>/`. Never overwrite a prior round. Keep raw
  specialist findings separate from the reconciled final review.
- Only lessons that survive reconciliation and apply beyond one PR go to `KnowledgeBase/`,
  with source PR and limits. Do not hand-edit generated indexes or profiles.

## Posting and remote operations

- Context gathering is read-only.
- Posting comments, pushing review branches, updating work items or completing PRs needs
  explicit user approval. Use the `sandbox-http` skill for REST calls.
- Verify before posting: preview the exact text, check the PR head has not moved, and read
  back every posted comment.
- Never expose credentials, tokens, authorization headers or secret environment values in
  logs, artifacts, command arguments or chat. Never bypass an egress denial.
- `merge_review.py` merges a finished review into the local review repository only. It does
  not push or touch the remote PR.

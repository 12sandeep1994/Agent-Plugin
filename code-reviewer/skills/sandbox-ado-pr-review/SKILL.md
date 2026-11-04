---
name: sandbox-ado-pr-review
description: Use FIRST when asked to review, and optionally post comments on, an Azure DevOps pull request from inside a sandbox where Azure DevOps MCP tools are unavailable. Supplies the GET-only PR fetch, frozen snapshot and comment-only publication scripts plus the safety rules, then drives this plugin's pr-review workflow with its specialist agents, review-grader and post-pr-review formatting.
---

# Sandbox Azure DevOps PR review

This skill is the Azure DevOps transport and publication layer for this plugin's review
workflow when `mcp__azure-devops__*` tools are not available (for example inside an NCS
sandbox). The review methodology comes from this plugin's own skills and agents.

## Resolve paths first

The Skill tool result for this skill starts with a header like
``Learning skill <name> from `<dir>/SKILL.md` ``. Use that actual `<dir>`; do not assume a
fixed mount name.

- `SKILL_DIR` = `<dir>` (this skill's directory).
- `PLUGIN_ROOT` = two levels above `SKILL_DIR` (for example `/skills/<sourceId>`).
- `<sourceId>:` = the prefix shown before `sandbox-ado-pr-review` in the Skill tool list.
- Resolve `${CLAUDE_PLUGIN_ROOT}` to `PLUGIN_ROOT` and `${CLAUDE_SKILL_DIR}` to
  `PLUGIN_ROOT/skills/<skill-name>` wherever plugin files use them, including in child
  briefings.

If `Read` cannot open a plugin path, use `Bash` with `cat`. The plugin mount is read-only:
never write under `/skills`. Write review output only under the returned review root.

## Workflow

1. Invoke the `pr-review` skill through the Skill tool with the same prefix
   (`<sourceId>:pr-review`) and follow it for scope, classification, dispatch,
   consolidation and grading. Load its references at the step that names them.
2. Invoke `<sourceId>:codebase-search-discipline` and apply it.
3. Before publishing, invoke `<sourceId>:post-pr-review` for formatting, priority and
   deduplication.

Every `mcp__azure-devops__*` call and every provider-resolution step in those skills is
replaced by the scripts below. Do not try to install or configure an ADO MCP server.

## Transport: fetch (GET only)

The scripts use the sandbox's managed egress. No PAT, token or credential is needed or
accepted in the sandbox; credentials stay with the gateway's external provider.

```
python3 SKILL_DIR/scripts/fetch_pr.py start 'THE_USER_PR_URL'
```

Accepted URL forms:

- `https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/{id}`
- `https://{org}.visualstudio.com/[DefaultCollection/]{project}/_git/{repo}/pullrequest/{id}`

No query, fragment, port, user info or encoded escapes. Organization, project and
repository come from the URL. There is no org/project allow-list in the script: the
gateway egress profile and the external credential provider policy are the boundary and
reject destinations they do not authorize. A provider denial is final; never retry it by
another route.

`start` returns an absolute `reviewRoot` under `/workspace/reviews/<PR_ID>/<REVIEW_ID>/`
with `request.json`, `snapshot.json`, `pr.diff`, `base/`, `source/` and `target/`, plus a
`review` provenance object. Diffs are merge-base to source, pinned to frozen 40-hex
commits. Every later command REQUIRES the exact returned root:

```
python3 SKILL_DIR/scripts/fetch_pr.py --review-root 'REVIEW_ROOT' metadata
python3 SKILL_DIR/scripts/fetch_pr.py --review-root 'REVIEW_ROOT' changes
python3 SKILL_DIR/scripts/fetch_pr.py --review-root 'REVIEW_ROOT' count
python3 SKILL_DIR/scripts/fetch_pr.py --review-root 'REVIEW_ROOT' file 'repository/path' 'FROZEN_COMMIT'
python3 SKILL_DIR/scripts/fetch_pr.py --review-root 'REVIEW_ROOT' comments
```

Never assume a current or previous PR. Each `start` creates a new isolated review root;
several PRs in one session each use their own root. State unavailable data (work items,
pipelines, other services) as a limitation; do not invent access.

## Review with real specialists

Specialist role files are `PLUGIN_ROOT/agents/<agent-id>.md`. Spawn each with the Agent
tool using `subagent_type: general-purpose`; give the role name and instruct the child to
Read its ENTIRE role file first, with placeholders resolved as above. Always include
`temp-code-review`, and run `review-grader` on the actual consolidated findings.

Brief every child with the PR ID, exact review root, frozen pins and diff path. Children
never post. Collect their actual results, consolidate and deduplicate, then grade.

## Publish after grading

Write `REVIEW_ROOT/publication.json`, copying `review` unchanged from the reader's output:

```json
{
  "review": {
    "pullRequestId": 123,
    "organization": "ACTUAL_ORGANIZATION",
    "source": "ACTUAL_40_CHARACTER_SOURCE_COMMIT",
    "target": "ACTUAL_40_CHARACTER_TARGET_COMMIT",
    "repository": {
      "id": "ACTUAL_REPOSITORY_GUID",
      "name": "ACTUAL_REPOSITORY_NAME",
      "project": {"id": "ACTUAL_PROJECT_GUID", "name": "ACTUAL_PROJECT_NAME"}
    }
  },
  "summary": "## PR Review: actual title\n\nVerdict, scope, pins, findings, actual child outcomes and limitations.",
  "findings": [
    {"file": "path/in/repo.cs", "line": 42, "content": "**HIGH** [BLOCKER]\n\nEvidence and suggested correction."}
  ]
}
```

Use an empty findings array for a clean review. A null line is a file-level comment; null
file and line is a general comment. Use source-side lines of actual changed paths.

Always preview first:

```
python3 SKILL_DIR/scripts/post_review.py --review-root 'REVIEW_ROOT' preview 'REVIEW_ROOT/publication.json'
```

Publish ONLY when the user explicitly asks to post this review:

```
python3 SKILL_DIR/scripts/post_review.py --review-root 'REVIEW_ROOT' publish 'REVIEW_ROOT/publication.json'
```

The script rechecks pins, posts findings and one marked summary thread, skips exact
duplicates, reads comments back and records `publish-receipt.json`.

## Safety rules (always apply)

- Always preview. Publish only on the user's explicit request to post this review.
- Only create comment threads and replies on the reviewed PR. Never vote, approve,
  complete or merge, push source, resolve or close threads, edit or delete anyone's
  comments, change work items, or change PR status, title, description or reviewers.
- No tokens, PATs or credentials in the sandbox. Never bypass, work around or retry a
  provider or egress denial through another route.
- Never write under `/skills`.
- Report actual receipts and readback results. Never claim success without readback; on
  failure, report the receipt and do not blindly retry.
- Use a separate review root for every PR.

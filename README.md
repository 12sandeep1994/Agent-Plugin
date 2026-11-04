# agent-plugins

Public agent plugins for use with an NCS SandboxTools MCP gateway.

## `code-reviewer`

`code-reviewer/` is a copy of the `code-reviewer` Claude plugin (see [NOTICE.md](NOTICE.md))
plus one extra skill, `sandbox-ado-pr-review`. That skill lets the plugin review, and
optionally comment on, Azure DevOps pull requests from inside a sandbox where Azure DevOps
MCP tools are unavailable. It provides:

- `scripts/fetch_pr.py`: a GET-only reader that freezes the PR's commits and diff under
  `/workspace/reviews/<PR>/<uuid>`;
- `scripts/post_review.py`: a comment-only publisher with preview, dedupe, readback and
  receipts.

Both use Python 3 stdlib only and accept no credentials. Authentication and the allowed
destinations are enforced outside the sandbox by the gateway egress profile and its
external credential provider.

## Register with an NCS gateway

Add a public plugin source to the gateway App with:

| Field | Value |
| --- | --- |
| repository | `https://github.com/12sandeep1994/agent-plugins` |
| path | `code-reviewer` |
| revision | the exact 40-hex commit SHA to load (no branch or tag) |

The gateway mounts the plugin read-only at `/skills/<sourceId>`. Its skills appear in the
Skill tool as `<sourceId>:<skill>`, for example `<sourceId>:sandbox-ado-pr-review` and
`<sourceId>:pr-review`. Agent role files are under `/skills/<sourceId>/agents/`.

To review an ADO PR in the sandbox, ask the agent to review the PR URL; the
`sandbox-ado-pr-review` skill is designed to be chosen first and drives the rest.
Publishing comments additionally needs an egress profile and credential provider that
allow comment POSTs to that PR.

## Development

Only `code-reviewer/` is shipped in the bundle; `tests/` and `tools/` are not.

```
python -m unittest discover -s tests -v
python tools/validate_plugin.py code-reviewer
```

`tools/validate_plugin.py` mirrors the gateway's public-plugin parser (bundle limits, path
rules, manifest, skill and agent discovery, frontmatter and name rules) and prints the
discovered skill and agent names.

---
name: sandbox-http
description: Use for any Azure DevOps or GitHub REST call from inside an NPD/NCS sandbox when no MCP tool covers it, for example posting review comments or reading PR threads. Wraps curl through the sandbox egress proxy, which adds credentials. Never send an Authorization header, token or cookie.
---

# Sandbox HTTP (Azure DevOps and GitHub REST)

`scripts/sandbox_fetch.py` sends one HTTPS request through the sandbox egress proxy.
The proxy adds credentials outside the sandbox. You never hold, send or ask for a token.

## Resolve the path

The Skill tool result starts with ``Learning skill <name> from `<dir>/SKILL.md` ``.
Use that `<dir>` as `SKILL_DIR`. The plugin mount is read-only: never write under it.

```
python3 SKILL_DIR/scripts/sandbox_fetch.py --url URL [--method GET|POST|PATCH|PUT] \
  [--header Name:Value ...] [--data-file BODY.json] [--output RESPONSE.json] [--timeout 120]
```

- Write a request body to a file under `/workspace` first, then pass `--data-file`.
  `--data-file -` reads the body from stdin.
- Use `--output` for large responses; the result then has `body_file` instead of `body`.
- Allowed: `https` only, hosts `dev.azure.com`, `*.visualstudio.com`, `api.github.com`,
  `github.com`. No redirects, no user info, no custom ports.
- Refused: `Authorization`, `Proxy-Authorization` and `Cookie` headers.

## Result

stdout is one JSON object:

```json
{"status": "ok", "http_status": 201, "method": "POST", "url": "...", "body": {"id": 42}}
```

| exit | status | meaning | what to do |
|---|---|---|---|
| 0 | `ok` | HTTP 2xx | Use `body`. For writes, read back to confirm. |
| 1 | `http_error` | other HTTP status from the service | Read `body`; fix the request. Do not loop. |
| 2 | `refused` | unsafe or invalid arguments; nothing sent | Fix the arguments. Never add credentials. |
| 10 | `denied` | egress policy denied the request | Final. Report it. Never retry by another route. |
| 11 | `auth_pending` | proxy is still getting credentials | Wait, then retry the same call a few times at most. |
| 12 | `transport_error` | curl failed or timed out | A write may or may not have happened. Read back before any retry. |

stderr has one short line only. Headers and bodies never go to stderr.

## Azure DevOps examples

Read PR threads:

```
python3 SKILL_DIR/scripts/sandbox_fetch.py --method GET \
  --url 'https://dev.azure.com/ORG/PROJECT/_apis/git/repositories/REPO/pullrequests/PR/threads?api-version=7.1'
```

Post a new comment thread (`body.json` = `{"comments":[{"parentCommentId":0,"content":"...","commentType":1}],"status":1}`):

```
python3 SKILL_DIR/scripts/sandbox_fetch.py --method POST \
  --url 'https://dev.azure.com/ORG/PROJECT/_apis/git/repositories/REPO/pullrequests/PR/threads?api-version=7.1' \
  --header Content-Type:application/json --data-file /workspace/tmp/body.json
```

Reply in a thread: POST `.../pullrequests/PR/threads/THREAD/comments?api-version=7.1` with
`{"parentCommentId":1,"content":"...","commentType":1}`.

## GitHub examples

Add `--header Accept:application/vnd.github+json` to every GitHub call.

- PR conversation comment: POST `https://api.github.com/repos/OWNER/REPO/issues/PR/comments`
  with `{"body":"..."}`.
- Inline review: POST `https://api.github.com/repos/OWNER/REPO/pulls/PR/reviews` with
  `{"commit_id":"HEAD_SHA","event":"COMMENT","body":"...","comments":[{"path":"a.py","line":12,"side":"RIGHT","body":"..."}]}`.
  List its inline comments with GET `.../pulls/PR/reviews/REVIEW_ID/comments`.
- Reply to an inline thread: POST `.../pulls/PR/comments/COMMENT_ID/replies` with `{"body":"..."}`.

## Rules

- Only post when the task or user explicitly asks you to post. Preview the text first.
- Comments only. Never approve, request changes, vote, complete, merge, push, close or
  resolve threads, or edit or delete anyone's comments.
- After every write, read it back and report the actual ids. Never claim success without readback.
- Never print, store or ask for a token. Never bypass a `denied` result.

"""Sandbox-only, GET-only reader for one Azure DevOps Git pull request.

No credentials are accepted. curl uses the sandbox-managed egress proxy.
The organization, project, repository and PR number come from the PR URL.
This script has no organization/project allow-list: the gateway egress
profile and the external credential provider policy are the boundary and
reject any destination they do not authorize. Provider denials are final.
The first snapshot freezes actual ADO source/target/common-commit identifiers.
"""

import argparse
import difflib
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
import urllib.parse
import uuid

ROOT = Path("/workspace/review")
REVIEWS_ROOT = Path("/workspace/reviews")
API_HOST = "https://dev.azure.com/"
SCHEMA_VERSION = 3
REQUEST_KEYS = {"schemaVersion", "pullRequestId", "organization", "project", "repository"}
GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
ORGANIZATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,49}")
VISUALSTUDIO_HOST = re.compile(r"([a-z0-9][a-z0-9-]{0,49})\.visualstudio\.com")
ENCODED_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})|%(?:00|25|2e|2f|5c)", re.I)


def component(value):
    if (not isinstance(value, str) or not value or value in (".", "..")
            or any(char in value for char in "/\\%?#")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("An unambiguous organization/project/repository name or GUID is required.")
    return value.lower() if GUID.fullmatch(value) else value


def organization(value):
    value = component(value)
    if not ORGANIZATION.fullmatch(value):
        raise ValueError("An Azure DevOps organization name is required.")
    return value


def request():
    value = json.loads((ROOT / "request.json").read_text())
    if (not isinstance(value, dict) or set(value) != REQUEST_KEYS
            or type(value["schemaVersion"]) is not int or value["schemaVersion"] != SCHEMA_VERSION):
        raise ValueError("Unrecognized immutable review request.")
    number = value["pullRequestId"]
    if type(number) is not int or not 0 < number <= 2147483647:
        raise ValueError("Positive PR identifier required.")
    organization(value["organization"])
    component(value["project"])
    component(value["repository"])
    return value


def _matches(selected, identifier, name):
    if GUID.fullmatch(selected):
        return identifier.lower() == selected.lower()
    return name.casefold() == selected.casefold()


def repository_identity(repository):
    """Check the ADO repository object against the request; return frozen identity."""
    selected = request()
    if not isinstance(repository, dict) or not isinstance(repository.get("project"), dict):
        raise ValueError("PR belongs to a different repository or project.")
    identifier = repository.get("id", "")
    project = repository["project"]
    project_id = project.get("id", "")
    if not isinstance(identifier, str) or not isinstance(project_id, str):
        raise ValueError("PR belongs to a different repository or project.")
    name = component(repository.get("name"))
    project_name = component(project.get("name"))
    if (not GUID.fullmatch(identifier) or not GUID.fullmatch(project_id)
            or not _matches(selected["project"], project_id, project_name)
            or not _matches(selected["repository"], identifier, name)):
        raise ValueError("PR belongs to a different repository or project.")
    return {"id": identifier.lower(), "name": name,
            "project": {"id": project_id.lower(), "name": project_name}}


def repository_prefix():
    selected = request()
    project, repository = selected["project"], selected["repository"]
    if (ROOT / "snapshot.json").exists():
        # Keep the project segment as the user supplied it: egress policies commonly allow
        # project paths by name, so switching to the project GUID would be denied.
        repository = repository_identity(snapshot()["pr"]["repository"])["id"]
    return (API_HOST + urllib.parse.quote(selected["organization"], safe="") + "/"
            + urllib.parse.quote(project, safe="") + "/_apis/git/repositories/"
            + urllib.parse.quote(repository, safe=""))


def review_provenance(frozen):
    return {"pullRequestId": frozen["pr"]["pullRequestId"],
            "organization": request()["organization"],
            "repository": frozen["pr"]["repository"],
            "source": frozen["source"], "target": frozen["target"]}


def get(route, **query):
    if not route.startswith("/") or ".." in route or "?" in route:
        raise ValueError("Invalid repository API route.")
    query["api-version"] = "7.1"
    url = repository_prefix() + route + "?" + urllib.parse.urlencode(query)
    deadline = time.monotonic() + 120
    attempt = 0
    while True:
        attempt += 1
        remaining = max(1, int(deadline - time.monotonic()))
        reply = subprocess.run(
            ["curl", "--silent", "--show-error", "--max-time", str(remaining),
             "--write-out", "\n%{http_code}", "--header", "Accept: application/json", url],
            capture_output=True, timeout=remaining + 5)
        if reply.returncode:
            raise RuntimeError(f"ADO GET failed (curl {reply.returncode}). "
                               "Check provider callback evidence; no fallback is used.")
        body, separator, code = reply.stdout.rpartition(b"\n")
        if not separator or not code.isdigit():
            raise RuntimeError("Sandbox HTTP status receipt is missing.")
        status = int(code)
        with (ROOT / "fetch-attempts.jsonl").open("a", encoding="utf-8") as evidence:
            evidence.write(json.dumps({"utcSeconds": int(time.time()), "route": route,
                                       "attempt": attempt, "status": status}) + "\n")
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("ADO response exceeds the 8 MiB per-response scope.")
        if status == 200:
            return json.loads(body)
        pending = status == 503 and any(
            marker in body.lower() for marker in (b"auth_pending", b"authentication pending"))
        if pending and time.monotonic() + 2 < deadline:
            time.sleep(2)
            continue
        raise RuntimeError(f"ADO GET failed (HTTP {status}). "
                           "No permission expansion, stale credential or fabricated result was used.")


def pr_id():
    number = request()["pullRequestId"]
    if ROOT.parent.parent == REVIEWS_ROOT.resolve() and ROOT.parent.name != str(number):
        raise ValueError("Selected review directory belongs to a different PR.")
    return number


def use_review(path):
    global ROOT
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("An explicit absolute review root is required.")
    ROOT = path.resolve()


def parse_pr_url(url):
    """Parse a dev.azure.com or {org}.visualstudio.com Git PR URL into a request."""
    if (not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or any(char in url for char in "\\?#")):
        raise ValueError("An ordinary HTTPS Azure DevOps PR URL is required.")
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    hosted = VISUALSTUDIO_HOST.fullmatch(host)
    if (parsed.scheme != "https" or (host != "dev.azure.com" and not hosted)
            or "@" in parsed.netloc or ":" in parsed.netloc
            or parsed.netloc != host
            or parsed.query or parsed.fragment):
        raise ValueError("An ordinary HTTPS Azure DevOps PR URL is required.")
    raw = parsed.path.removeprefix("/").removesuffix("/").split("/")
    if any(ENCODED_ESCAPE.search(part) for part in raw):
        raise ValueError("Encoded path escapes are not allowed.")
    parts = [component(urllib.parse.unquote(part, errors="strict")) for part in raw]
    if hosted:
        org = hosted.group(1)
        if parts and parts[0].casefold() == "defaultcollection":
            parts.pop(0)
    else:
        if not parts:
            raise ValueError("An Azure DevOps organization is required in the URL.")
        org = parts.pop(0)
    org = organization(org)
    if (len(parts) != 5 or parts[1].casefold() != "_git"
            or parts[3].casefold() != "pullrequest"):
        raise ValueError("Only Azure DevOps Git pull request URLs are allowed.")
    number = parts[-1]
    if not number.isascii() or not number.isdigit() or not 0 < int(number) <= 2147483647:
        raise ValueError("A positive PR number is required in the URL.")
    return {"schemaVersion": SCHEMA_VERSION, "pullRequestId": int(number),
            "organization": org, "project": parts[0], "repository": parts[2]}


def start(url, *, review_root=None):
    selected = parse_pr_url(url)
    number = selected["pullRequestId"]
    use_review(review_root if review_root is not None
               else REVIEWS_ROOT / str(number) / uuid.uuid4().hex)
    ROOT.mkdir(parents=True, exist_ok=True)
    request_path = ROOT / "request.json"
    if request_path.exists():
        if pr_id() != number:
            raise ValueError("This review root belongs to another PR. Start a separate review in the same workspace.")
        previous = request()
        if not all(previous[key].casefold() == selected[key].casefold()
                   for key in ("organization", "project", "repository")):
            raise ValueError("This review root belongs to a different repository or project.")
        if (ROOT / "snapshot.json").exists():
            metadata()
            frozen = completed_snapshot()
            return {**frozen, "review": review_provenance(frozen), "reviewRoot": str(ROOT)}
    else:
        request_path.write_text(json.dumps(selected), encoding="utf-8")
    return {**prepare(), "reviewRoot": str(ROOT)}


def pin(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{40}", value):
        raise ValueError("Actual ADO commit pin is missing.")
    return value


def metadata():
    pr = get(f"/pullrequests/{pr_id()}")
    if pr["pullRequestId"] != pr_id():
        raise ValueError("PR belongs to a different PR.")
    repository = repository_identity(pr["repository"])
    source = pin(pr["lastMergeSourceCommit"]["commitId"])
    target = pin(pr["lastMergeTargetCommit"]["commitId"])
    snapshot_path = ROOT / "snapshot.json"
    if snapshot_path.exists():
        frozen = snapshot()
        if repository != frozen["pr"].get("repository"):
            raise ValueError("PR repository identity changed after snapshot.")
        if source != frozen["source"] or target != frozen["target"]:
            raise ValueError("PR advanced after snapshot. Start a fresh review.")
    value = {key: pr.get(key) for key in
            ("pullRequestId", "title", "description", "status", "sourceRefName",
             "targetRefName", "lastMergeSourceCommit", "lastMergeTargetCommit")}
    value["repository"] = repository
    return value


def snapshot():
    value = json.loads((ROOT / "snapshot.json").read_text())
    if value["pr"]["pullRequestId"] != pr_id():
        raise ValueError("Frozen snapshot belongs to a different PR.")
    repository_identity(value["pr"]["repository"])
    return value


def completed_snapshot():
    marker = ROOT / "complete.json"
    if not marker.exists():
        raise ValueError("Review collection is incomplete. Start a new review; partial inputs are not a review.")
    hashes = json.loads(marker.read_text())
    frozen = snapshot()
    expected = {"snapshot.json", "pr.diff", "request.json"} | {
        f"{tree}/{path}" for tree in ("base", "source") for path in frozen["changedFiles"]}
    if set(hashes) != expected or any(
            not (ROOT / path).is_file()
            or hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != hashes[path]
            for path in expected):
        raise ValueError("Review collection is incomplete or changed. Start a new review.")
    return frozen


def repository_path(path):
    path = path.lstrip("/")
    if (not path or "\\" in path or any(ord(char) < 32 for char in path)
            or any(part in ("", ".", "..") for part in path.split("/"))
            or PurePosixPath(path).is_absolute()):
        raise ValueError("Unambiguous repository-relative path required.")
    return path


def file(path, commit, *, tree=None):
    path = repository_path(path)
    frozen = snapshot()
    if commit not in (frozen["source"], frozen["target"], frozen["mergeBase"]):
        raise ValueError("File version must be one of this review's frozen pins.")
    item = get("/items", path="/" + path, **{
        "versionDescriptor.version": commit,
        "versionDescriptor.versionType": "commit", "includeContent": "true"})
    content = item.get("content")
    if not isinstance(content, str):
        raise ValueError("Only bounded text-file reviews are supported, not binary files.")
    tree = tree or ("source" if commit == frozen["source"]
                    else "base" if commit == frozen["mergeBase"] else "target")
    destination = ROOT / tree / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return {"path": str(destination), "commit": commit,
            "lines": len(content.splitlines()), "bytes": len(content.encode())}


def prepare():
    if (ROOT / "snapshot.json").exists():
        raise ValueError("A frozen snapshot cannot be replaced. Start a fresh review.")
    pr = metadata()
    source = pr["lastMergeSourceCommit"]["commitId"]
    target = pr["lastMergeTargetCommit"]["commitId"]
    diff = get("/diffs/commits", **{
        "baseVersion": target, "baseVersionType": "commit",
        "targetVersion": source, "targetVersionType": "commit",
        "diffCommonCommit": "true", "$top": "100"})
    if diff.get("allChangesIncluded") is not True:
        raise ValueError("Review scope exceeds 100 changes. Narrower manual review required.")
    changes = []
    for change in diff["changes"]:
        item = change["item"]
        if item.get("isFolder") or item.get("gitObjectType") == "tree":
            continue
        kind = change["changeType"].lower()
        if kind not in ("add", "edit", "delete"):
            raise ValueError("Rename/compound change types are not supported by this local profile.")
        changes.append({"path": repository_path(item["path"]), "kind": kind})
    if not changes:
        raise ValueError("No supported changed text files to review.")
    value = {"pr": pr, "source": source, "target": target,
             "mergeBase": pin(diff["commonCommit"]),
             "changedFiles": sorted(item["path"] for item in changes),
             "changes": changes,
             "retrievedBy": "sandbox curl through managed gateway egress",
             "workItemContext": "unavailable: not included in immutable GET policy"}
    (ROOT / "snapshot.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
    pieces = []
    total = 0
    for change in changes:
        path = change["path"]
        texts = []
        for tree, commit, absent in (
                ("base", value["mergeBase"], change["kind"] == "add"),
                ("source", source, change["kind"] == "delete")):
            destination = ROOT / tree / path
            if absent:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("", encoding="utf-8")
            else:
                file(path, commit, tree=tree)
            text = destination.read_text(encoding="utf-8")
            total += len(text.encode())
            if total > 32 * 1024 * 1024:
                raise ValueError("Review input exceeds the bounded 32 MiB text scope.")
            texts.append(text)
        pieces.extend(difflib.unified_diff(
            texts[0].splitlines(keepends=True), texts[1].splitlines(keepends=True),
            fromfile="a/" + path, tofile="b/" + path))
    metadata()  # Do not silently mix versions if the PR moved during collection.
    (ROOT / "pr.diff").write_text("".join(pieces), encoding="utf-8")
    # This atomic marker is committed only after every pinned input and the diff succeeded.
    inputs = ["snapshot.json", "pr.diff", "request.json"] + [
        f"{tree}/{path}" for tree in ("base", "source") for path in value["changedFiles"]]
    marker = ROOT / "complete.tmp"
    marker.write_text(json.dumps({
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in inputs}), encoding="utf-8")
    marker.replace(ROOT / "complete.json")
    return {**value, "review": review_provenance(value)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", type=Path)
    parser.add_argument("operation", choices=("start", "snapshot", "metadata", "changes", "count", "file", "comments"))
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args(argv)
    operation = args.operation
    expected = (1, 2) if operation == "file" else (1,) if operation == "start" else (0,)
    if len(args.arguments) not in expected:
        parser.error("Unexpected arguments for the selected operation.")
    if operation == "start":
        return start(args.arguments[0], review_root=args.review_root)
    if args.review_root is None:
        parser.error("Select --review-root from start's reviewRoot; no current PR is assumed.")
    use_review(args.review_root)
    if operation == "snapshot":
        value = prepare()
    elif operation == "metadata":
        value = metadata()
    elif operation == "changes":
        value = {"changedFiles": completed_snapshot()["changedFiles"], "diff": str(ROOT / "pr.diff")}
    elif operation == "count":
        changes = completed_snapshot()["changes"]
        value = {kind: sum(item["kind"] == key for item in changes)
                 for kind, key in (("modified", "edit"), ("added", "add"), ("deleted", "delete"))}
    elif operation == "file":
        value = file(args.arguments[0], args.arguments[1] if len(args.arguments) == 2 else snapshot()["source"])
    elif operation == "comments":
        value = get(f"/pullrequests/{pr_id()}/threads")
    return {**value, "reviewRoot": str(ROOT)}


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False))

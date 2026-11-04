"""Comment-only sandbox publisher for one Azure DevOps pull request.

Credentials remain outside the sandbox. Only comment threads and comment
replies are POSTed, to the PR frozen by fetch_pr.py in the selected review
root. There is no organization/project allow-list here: the gateway egress
profile and the external credential provider policy are the boundary.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_pr as reader

SUMMARY = "<!-- workspace-review-summary:v1 -->"


def threads():
    value = reader.get(f"/pullrequests/{reader.pr_id()}/threads")
    result = value.get("value")
    if not isinstance(result, list):
        raise ValueError("ADO thread listing is not complete JSON.")
    return result


def locate(entries, marker):
    matches = [(thread, comment) for thread in entries for comment in thread.get("comments", [])
               if marker in comment.get("content", "") and not comment.get("isDeleted")]
    if len(matches) > 1:
        raise ValueError("Duplicate publication markers need manual reconciliation.")
    return matches[0] if matches else None


def post(route, body):
    url = reader.repository_prefix() + route + "?api-version=7.1"
    response = subprocess.run(
        ["curl", "--silent", "--show-error", "--max-time", "120",
         "--request", "POST", "--header", "Content-Type: application/json",
         "--header", "Accept: application/json", "--data-binary", "@-",
         "--write-out", "\n%{http_code}", url],
        input=json.dumps(body).encode(), capture_output=True, timeout=125)
    content, separator, code = response.stdout.rpartition(b"\n")
    if response.returncode or not separator or code not in (b"200", b"201"):
        raise RuntimeError("ADO POST did not return confirmed success. Reconcile before retrying.")
    if len(content) > 8 * 1024 * 1024:
        raise ValueError("ADO POST response exceeds its bound.")
    return json.loads(content)


def checked_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 20000:
        raise ValueError("Nonempty publication text of at most 20000 characters is required.")
    if "<!-- workspace-review-" in value:
        raise ValueError("Publication markers are reserved.")
    return value.strip()


def file_contexts(files, source):
    if not files:
        return {}
    iterations = reader.get(f"/pullrequests/{reader.pr_id()}/iterations")["value"]
    matching = [item["id"] for item in iterations
                if item.get("sourceRefCommit", {}).get("commitId") == source
                and type(item.get("id")) is int and 0 < item["id"] <= 32767]
    if not matching:
        raise ValueError("The frozen source has no actual supported PR iteration.")
    iteration = max(matching)
    changes = reader.get(
        f"/pullrequests/{reader.pr_id()}/iterations/{iteration}/changes",
        **{"$compareTo": "0", "$top": "2000"})
    entries = changes.get("changeEntries")
    if (not isinstance(entries, list) or changes.get("nextSkip", 0)
            or changes.get("nextTop", 0)):
        raise ValueError("Iteration file changes are incomplete; publication stopped.")
    contexts = {}
    for file in files:
        matches = [item for item in entries if item.get("item", {}).get("path") == "/" + file]
        if (len(matches) != 1 or type(matches[0].get("changeTrackingId")) is not int
                or not 0 < matches[0]["changeTrackingId"] <= 2147483647):
            raise ValueError("File has no unique actual iteration change tracking identifier.")
        contexts[file] = {
            "changeTrackingId": matches[0]["changeTrackingId"],
            "iterationContext": {
                "firstComparingIteration": iteration, "secondComparingIteration": iteration}}
    return contexts


def prepare(report):
    frozen = reader.completed_snapshot()
    if not isinstance(report, dict) or set(report) != {"review", "summary", "findings"}:
        raise ValueError("Publication requires review provenance, summary and findings.")
    expected = reader.review_provenance(frozen)
    if (not isinstance(report["review"], dict)
            or type(report["review"].get("pullRequestId")) is not int or report["review"] != expected):
        raise ValueError("Publication does not match the selected PR and frozen source/target pins.")
    reader.metadata()
    if not isinstance(report["findings"], list) or len(report["findings"]) > 30:
        raise ValueError("At most 30 consolidated findings may be published.")
    entries = []
    for finding in report["findings"]:
        if not isinstance(finding, dict) or set(finding) != {"file", "line", "content"}:
            raise ValueError("Each finding requires file, line and content.")
        file, line = finding["file"], finding["line"]
        if file is not None:
            if not isinstance(file, str):
                raise ValueError("A finding file must be a repository-relative string or null.")
            file = reader.repository_path(file)
            if file not in frozen["changedFiles"]:
                raise ValueError("A finding must reference an actually changed file.")
        if line is not None:
            if file is None or type(line) is not int or line < 1:
                raise ValueError("Inline comments require a positive source-side line and changed file.")
            if line > len((reader.ROOT / "source" / file).read_text().splitlines()):
                raise ValueError("Inline line is outside the frozen source file.")
        entries.append({"file": file, "line": line, "content": checked_text(finding["content"])})
    entries.append({"file": None, "line": None, "content": checked_text(report["summary"]), "summary": True})
    for entry in entries:
        key = hashlib.sha256(json.dumps(
            [frozen["source"], frozen["target"], entry], sort_keys=True).encode()).hexdigest()
        entry["marker"] = "<!-- workspace-review-entry:v1:" + key + " -->"
    if len({entry["marker"] for entry in entries}) != len(entries):
        raise ValueError("Duplicate consolidated findings must be removed before publication.")
    contexts = file_contexts({entry["file"] for entry in entries if entry["file"] is not None}, frozen["source"])
    for entry in entries:
        if entry["file"] is not None:
            entry["pullRequestThreadContext"] = contexts[entry["file"]]
    reader.metadata()
    return entries


def publish(report, *, preview=False):
    entries = prepare(report)
    if preview:
        return {"status": "preview", "entries": entries, "remote_writes": 0,
                "reviewRoot": str(reader.ROOT), "review": report["review"]}
    lock_root = (reader.ROOT.parent if reader.ROOT.parent.parent == reader.REVIEWS_ROOT.resolve()
                 else reader.ROOT)
    lock = lock_root / "publish.lock"
    lock.mkdir()
    receipt = {"status": "publishing", "comments": [], "pullRequestId": reader.pr_id(),
               "reviewRoot": str(reader.ROOT), "review": report["review"]}
    receipt_path = reader.ROOT / "publish-receipt.json"
    try:
        for entry in entries:
            reader.completed_snapshot()
            reader.metadata()
            current = threads()
            existing = locate(current, entry["marker"])
            if existing:
                thread, comment = existing
                receipt["comments"].append({"threadId": thread["id"], "commentId": comment["id"], "status": "existing"})
                continue
            summary_threads = [thread for thread in current
                               if any(SUMMARY in comment.get("content", "")
                                      and not comment.get("isDeleted") for comment in thread.get("comments", []))]
            if len(summary_threads) > 1:
                raise ValueError("Multiple summary threads need manual reconciliation.")
            text = "[Workspace Review]\n\n" + entry["content"] + "\n\n" + entry["marker"]
            if entry.get("summary"):
                text += "\n" + SUMMARY
            comment_body = {"parentCommentId": 0, "content": text, "commentType": 1}
            route = f"/pullrequests/{reader.pr_id()}/threads"
            body = {"comments": [comment_body], "status": 1}
            if entry.get("summary") and summary_threads:
                route += "/" + str(summary_threads[0]["id"]) + "/comments"
                comment_body["parentCommentId"] = summary_threads[0]["comments"][0]["id"]
                body = comment_body
            elif entry["file"] is not None:
                context = {"filePath": "/" + entry["file"]}
                if entry["line"] is not None:
                    position = {"line": entry["line"], "offset": 1}
                    context.update(rightFileStart=position, rightFileEnd=position)
                body["pullRequestThreadContext"] = entry["pullRequestThreadContext"]
                body["threadContext"] = context
            attempt = {"route": route, "marker": entry["marker"]}
            with (reader.ROOT / "publish-attempts.jsonl").open("a") as output:
                output.write(json.dumps(attempt) + "\n")
            try:
                post(route, body)
            except (RuntimeError, ValueError, subprocess.SubprocessError) as error:
                recovered = locate(threads(), entry["marker"])
                if not recovered:
                    raise RuntimeError("Publication uncertain or rejected; existing comments retained. Inspect receipt before retrying.") from error
            actual = locate(threads(), entry["marker"])
            if not actual:
                raise RuntimeError("ADO comment success could not be verified by readback.")
            thread, comment = actual
            receipt["comments"].append({"threadId": thread["id"], "commentId": comment["id"], "status": "posted"})
        receipt["status"] = "posted"
        return receipt
    except Exception as error:
        receipt["status"] = "incomplete"
        receipt["error"] = str(error)
        raise
    finally:
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        lock.rmdir()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("operation", choices=("preview", "publish"))
    parser.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    reader.use_review(args.review_root)
    if args.report.resolve().parent != reader.ROOT:
        raise ValueError("The publication report must belong to the explicitly selected review root.")
    return publish(json.loads(args.report.read_text()), preview=args.operation == "preview")


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))

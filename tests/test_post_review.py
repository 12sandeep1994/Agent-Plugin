"""Publication contracts; only ADO transport is scripted. No network.

All organizations, projects, repositories and GUIDs below are fictional.
"""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

SCRIPTS = Path(__file__).resolve().parent.parent / "code-reviewer" / "skills" / "sandbox-ado-pr-review" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import fetch_pr as reader  # noqa: E402
import post_review as publisher  # noqa: E402

BASE_URL = "https://dev.azure.com/contoso/Fabrikam/_git/"
REPOSITORY_ID = "99999999-8888-7777-6666-555555555555"
PREFIX = "https://dev.azure.com/contoso/Fabrikam/_apis/git/repositories/Widgets"


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".logs" / "ado-publication-contract" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.source, self.target, self.base = "1" * 40, "2" * 40, "3" * 40
        self.pr = {"pullRequestId": 123, "repository": {
                       "id": REPOSITORY_ID, "name": "Widgets",
                       "project": {"id": "11111111-2222-3333-4444-555555555555", "name": "Fabrikam"}},
                   "lastMergeSourceCommit": {"commitId": self.source},
                   "lastMergeTargetCommit": {"commitId": self.target}}
        self.remote, self.writes = [], []
        self.prs, self.remotes = {123: self.pr}, {123: self.remote}
        for item in (patch.object(reader, "ROOT", self.root),
                     patch.object(reader, "get", side_effect=self.get),
                     patch.object(publisher, "post", side_effect=self.post)):
            item.start()
            self.addCleanup(item.stop)
        reader.start(BASE_URL + "Widgets/pullrequest/123", review_root=self.root)
        self.report = {"review": reader.review_provenance(reader.completed_snapshot()),
                       "summary": "Graded review with actual scope and limitations.",
                       "findings": [{"file": "file.md", "line": 2, "content": "Evidence and correction."}]}

    def get(self, route, **query):
        pr = self.prs[int(route.split("/")[2])] if route.startswith("/pullrequests/") else self.pr
        if route.endswith("/threads"):
            return {"value": copy.deepcopy(self.remotes[pr["pullRequestId"]])}
        if route.endswith("/iterations"):
            return {"value": [{"id": 7, "sourceRefCommit": pr["lastMergeSourceCommit"]}]}
        if route.endswith("/iterations/7/changes"):
            return {"changeEntries": [{"changeTrackingId": 31, "item": {"path": "/file.md"}}],
                    "nextSkip": 0, "nextTop": 0}
        if route.startswith("/pullrequests/"):
            return pr
        if route == "/diffs/commits":
            return {"allChangesIncluded": True, "commonCommit": self.base,
                    "changes": [{"item": {"path": "/file.md"}, "changeType": "edit"}]}
        return {"content": "first\n" + query["versionDescriptor.version"] + "\n"}

    def post(self, route, body):
        self.writes.append((route, copy.deepcopy(body)))
        remote = self.remotes[int(route.split("/")[2])]
        if route.endswith("/comments"):
            thread = next(t for t in remote if t["id"] == int(route.split("/")[-2]))
            thread["comments"].append({**body, "id": len(thread["comments"]) + 1})
        else:
            remote.append({**body, "id": len(remote) + 1,
                           "comments": [{**body["comments"][0], "id": 1}]})

    def test_preview_inline_post_readback_and_reentry_summary_reply(self):
        self.assertEqual(0, publisher.publish(self.report, preview=True)["remote_writes"])
        self.assertEqual([], self.writes)
        result = publisher.publish(self.report)
        self.assertEqual("posted", result["status"])
        self.assertEqual(2, len(self.writes))
        self.assertEqual(2, self.writes[0][1]["threadContext"]["rightFileStart"]["line"])
        self.assertEqual(31, self.writes[0][1]["pullRequestThreadContext"]["changeTrackingId"])
        self.assertEqual(7, self.writes[0][1]["pullRequestThreadContext"]["iterationContext"]["firstComparingIteration"])
        self.assertEqual(7, self.writes[0][1]["pullRequestThreadContext"]["iterationContext"]["secondComparingIteration"])
        repeated = publisher.publish(self.report)
        self.assertTrue(all(item["status"] == "existing" for item in repeated["comments"]))
        self.assertEqual(2, len(self.writes))
        self.report["summary"] = "Updated graded summary."
        publisher.publish(self.report)
        self.assertEqual(3, len(self.writes))
        self.assertEqual("/pullrequests/123/threads/2/comments", self.writes[-1][0])
        self.assertEqual(2, len(self.remote))

    def test_lost_post_response_is_reconciled_without_reposting(self):
        def uncertain(route, body):
            self.post(route, body)
            raise RuntimeError("Scripted lost response after remote commit.")
        with patch.object(publisher, "post", side_effect=uncertain):
            self.assertEqual("posted", publisher.publish(self.report)["status"])
        self.assertEqual(2, len(self.writes))
        publisher.publish(self.report)
        self.assertEqual(2, len(self.writes))

    def test_file_level_context_and_missing_or_incomplete_file_mapping(self):
        self.report["findings"][0]["line"] = None
        preview = publisher.publish(self.report, preview=True)
        self.assertEqual(31, preview["entries"][0]["pullRequestThreadContext"]["changeTrackingId"])
        self.assertEqual([], self.writes)
        for reply in ({"changeEntries": []},
                      {"changeEntries": [{"changeTrackingId": 31, "item": {"path": "/file.md"}}],
                       "nextSkip": 2000, "nextTop": 2000}):
            def missing(route, **query):
                return reply if route.endswith("/changes") else self.get(route, **query)
            with self.subTest(reply=reply), patch.object(reader, "get", side_effect=missing):
                with self.assertRaises(ValueError):
                    publisher.publish(self.report)
            self.assertEqual([], self.writes)

    def test_denied_or_unconfirmed_post_stops_and_persists_partial_state(self):
        for error, message in ((RuntimeError("HTTP403"), "uncertain or rejected"),
                               (None, "could not be verified")):
            with self.subTest(message=message), patch.object(publisher, "post", side_effect=error) as post:
                with self.assertRaisesRegex(RuntimeError, message):
                    publisher.publish(self.report)
                self.assertEqual(1, post.call_count)
        receipt = json.loads((self.root / "publish-receipt.json").read_text())
        self.assertEqual("incomplete", receipt["status"])
        self.assertEqual([], receipt["comments"])
        self.assertFalse((self.root / "publish.lock").exists())

    def test_summary_only_publication_and_conflicting_remote_markers(self):
        self.report["findings"] = []
        self.assertEqual("posted", publisher.publish(self.report)["status"])
        self.assertEqual(1, len(self.writes))
        self.assertNotIn("threadContext", self.writes[0][1])
        self.remote.append({**copy.deepcopy(self.remote[0]), "id": 99})
        with self.assertRaisesRegex(ValueError, "Duplicate publication markers"):
            publisher.publish(self.report)
        self.assertEqual(1, len(self.writes))

    def test_invalid_publication_input_never_writes(self):
        finding = self.report["findings"][0]
        invalid = [
            {},
            {"summary": "Valid", "findings": [finding] * 31},
            {"summary": "Valid", "findings": [{}]},
            {"summary": "", "findings": []},
            {"summary": "<!-- workspace-review-summary:v1 -->", "findings": []},
            {"summary": "Valid", "findings": [finding, finding]},
        ]
        for field, value in (("file", 123), ("file", "other.md"), ("line", True),
                             ("content", "x" * 20001)):
            invalid.append({"summary": "Valid", "findings": [{**finding, field: value}]})
        for index, value in enumerate(invalid):
            if set(value) == {"summary", "findings"}:
                value = {**value, "review": self.report["review"]}
            with self.subTest(case=index), self.assertRaises(ValueError):
                publisher.publish(value)
            self.assertEqual([], self.writes)

    def test_multiple_prs_publish_only_the_selected_report_and_keep_separate_receipts(self):
        self.prs[456] = {**copy.deepcopy(self.pr), "pullRequestId": 456}
        self.remotes[456] = []
        second = self.root / "second"
        reader.start(BASE_URL + "Widgets/pullrequest/456", review_root=second)
        with self.assertRaisesRegex(ValueError, "selected PR"):
            publisher.publish(self.report)
        self.assertEqual([], self.writes)
        report_b = {**self.report, "review": {**self.report["review"], "pullRequestId": 456}}
        path_b = second / "publication.json"
        path_b.write_text(json.dumps(report_b))
        publisher.main(["--review-root", str(second), "publish", str(path_b)])
        self.assertTrue(all(route.startswith("/pullrequests/456/") for route, _ in self.writes))
        receipt_b = (second / "publish-receipt.json").read_bytes()
        path_a = self.root / "publication.json"
        path_a.write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, "explicitly selected review root"):
            publisher.main(["--review-root", str(second), "publish", str(path_a)])
        publisher.main(["--review-root", str(self.root), "publish", str(path_a)])
        self.assertEqual(4, len(self.writes))
        self.assertTrue(all(route.startswith("/pullrequests/123/") for route, _ in self.writes[2:]))
        self.assertEqual(receipt_b, (second / "publish-receipt.json").read_bytes())
        publisher.main(["--review-root", str(second), "publish", str(path_b)])
        self.assertEqual(4, len(self.writes))
        self.assertEqual(123, json.loads((self.root / "publish-receipt.json").read_text())["pullRequestId"])
        self.assertEqual(456, json.loads((second / "publish-receipt.json").read_text())["pullRequestId"])

    def test_stale_pins_invalid_anchors_and_modified_snapshot_stop_before_writes(self):
        self.report["findings"][0]["line"] = 999
        with self.assertRaisesRegex(ValueError, "outside"):
            publisher.publish(self.report)
        self.report["findings"][0]["line"] = 2
        self.pr["lastMergeSourceCommit"]["commitId"] = "4" * 40
        with self.assertRaisesRegex(ValueError, "advanced"):
            publisher.publish(self.report)
        self.pr["lastMergeSourceCommit"]["commitId"] = self.source
        (self.root / "source/file.md").write_text("modified")
        with self.assertRaisesRegex(ValueError, "changed"):
            publisher.publish(self.report)
        self.assertEqual([], self.writes)

    def test_repository_provenance_is_required_even_when_pr_and_commit_pins_match(self):
        self.pr["repository"] = {**self.pr["repository"],
                                 "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "name": "Gadgets"}
        second = self.root / "gadgets"
        reader.start(BASE_URL + "Gadgets/pullrequest/123", review_root=second)
        with self.assertRaisesRegex(ValueError, "selected PR"):
            publisher.publish(self.report)
        self.assertEqual([], self.writes)
        report = {**self.report, "review": reader.review_provenance(reader.completed_snapshot())}
        self.assertEqual(0, publisher.publish(report, preview=True)["remote_writes"])
        self.assertTrue(reader.repository_prefix().endswith("/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
        # The project stays as named in the PR URL so name-scoped egress policies still match.
        self.assertTrue(reader.repository_prefix().startswith(
            "https://dev.azure.com/contoso/Fabrikam/_apis/git/repositories/"))


class TransportTests(unittest.TestCase):
    def test_publisher_uses_the_sibling_reader_module(self):
        self.assertIs(reader, publisher.reader)
        self.assertEqual(SCRIPTS / "fetch_pr.py", Path(publisher.reader.__file__).resolve())

    def test_only_confirmed_bounded_json_http_success_is_accepted(self):
        cases = [
            (0, b"{}\n403", RuntimeError),
            (1, b"{}\n201", RuntimeError),
            (0, b"no status", RuntimeError),
            (0, b"invalid json\n201", ValueError),
            (0, b"x" * (8 * 1024 * 1024 + 1) + b"\n201", ValueError),
        ]
        for returncode, output, error in cases:
            response = publisher.subprocess.CompletedProcess([], returncode, stdout=output)
            with self.subTest(returncode=returncode, size=len(output)):
                with patch.object(publisher.subprocess, "run", return_value=response), \
                        patch.object(reader, "repository_prefix", return_value=PREFIX):
                    with self.assertRaises(error):
                        publisher.post("/pullrequests/123/threads", {})


if __name__ == "__main__":
    unittest.main()

"""Reader contracts: URL parsing, request schema, identity, pins and collection; no network.

All organizations, projects, repositories and GUIDs below are fictional.
"""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

SCRIPTS = Path(__file__).resolve().parent.parent / "code-reviewer" / "skills" / "sandbox-ado-pr-review" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import fetch_pr as READER  # noqa: E402

URL = "https://dev.azure.com/contoso/Fabrikam/_git/Widgets/pullrequest/123"
COLLECTION = "Default" + "Collection"  # kept split so repository hygiene greps stay meaningful
PROJECT = "11111111-2222-3333-4444-555555555555"
REPOSITORY_ID = "99999999-8888-7777-6666-555555555555"
REPOSITORY = {"id": REPOSITORY_ID, "name": "Widgets",
              "project": {"id": PROJECT, "name": "Fabrikam"}}


class ReviewReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path.cwd() / ".logs" / "sandbox-ado-pr-review-reader" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_multiple_prs_and_revisions_keep_separate_immutable_review_contexts(self):
        source = "1" * 40

        def get(route, **query):
            if route.startswith("/pullrequests/"):
                number = int(route.split("/")[2])
                return {"pullRequestId": number, "repository": REPOSITORY,
                        "lastMergeSourceCommit": {"commitId": source if number == 123 else "4" * 40},
                        "lastMergeTargetCommit": {"commitId": "2" * 40}}
            if route == "/diffs/commits":
                return {"allChangesIncluded": True, "commonCommit": "3" * 40,
                        "changes": [{"item": {"path": "/file.md"}, "changeType": "edit"}]}
            return {"content": "actual " + query["versionDescriptor.version"] + "\n"}

        with patch.object(READER, "ROOT", self.directory), patch.object(
                READER, "REVIEWS_ROOT", self.directory / "reviews"), \
                patch.object(READER, "get", side_effect=get):
            first = READER.start(URL)
            second = READER.start(URL.replace("/123", "/456"))
            first_root, second_root = Path(first["reviewRoot"]), Path(second["reviewRoot"])
            self.assertNotEqual(first_root, second_root)
            self.assertEqual((123, 456), (first["pr"]["pullRequestId"], second["pr"]["pullRequestId"]))
            original = {path.name: path.read_bytes() for path in first_root.iterdir() if path.is_file()}
            self.assertEqual(first, READER.start(URL, review_root=first_root))
            source = "5" * 40
            latest = READER.start(URL)
            self.assertNotEqual(first_root, Path(latest["reviewRoot"]))
            self.assertEqual("5" * 40, latest["source"])
            self.assertEqual(original, {path.name: path.read_bytes() for path in first_root.iterdir() if path.is_file()})
            READER.use_review(first_root)
            with self.assertRaisesRegex(ValueError, "advanced"):
                READER.metadata()
            self.assertEqual("1" * 40, READER.completed_snapshot()["source"])

    def test_completed_inputs_reenter_and_target_reads_never_replace_merge_base(self):
        source, target, common = "1" * 40, "2" * 40, "3" * 40
        pr = {"pullRequestId": 123, "repository": REPOSITORY,
              "lastMergeSourceCommit": {"commitId": source},
              "lastMergeTargetCommit": {"commitId": target}}

        def get(route, **query):
            if route == "/pullrequests/123":
                return pr
            if route == "/diffs/commits":
                return {"allChangesIncluded": True, "commonCommit": common,
                        "changes": [{"item": {"path": "/file.md"}, "changeType": "edit"}]}
            return {"content": "actual " + query["versionDescriptor.version"] + "\n"}

        with patch.object(READER, "ROOT", self.directory), patch.object(READER, "get", side_effect=get):
            frozen = READER.start(URL, review_root=self.directory)
            self.assertEqual(frozen, READER.start(URL, review_root=self.directory))
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as missing:
                READER.main(["metadata"])
            self.assertEqual(2, missing.exception.code)
            self.assertEqual(123, READER.main(["--review-root", str(self.directory), "metadata"])["pullRequestId"])
            root = self.directory
            base, diff = (root / "base/file.md").read_bytes(), (root / "pr.diff").read_bytes()
            READER.file("file.md", target)
            self.assertEqual(base, (root / "base/file.md").read_bytes())
            self.assertEqual(diff, (root / "pr.diff").read_bytes())
            self.assertEqual(frozen, READER.start(URL, review_root=self.directory))
            (root / "base/file.md").write_text("wrong tree")
            with self.assertRaisesRegex(ValueError, "changed"):
                READER.start(URL, review_root=self.directory)
            other = READER.parse_pr_url(URL.replace("/123", "/456"))
            (root / "request.json").write_text(json.dumps(other))
            with self.assertRaisesRegex(ValueError, "different PR"):
                READER.completed_snapshot()

    def test_interrupted_item_download_cannot_reenter_as_complete_snapshot(self):
        source, target, common = "1" * 40, "2" * 40, "3" * 40
        pr = {"pullRequestId": 123, "repository": REPOSITORY,
              "lastMergeSourceCommit": {"commitId": source},
              "lastMergeTargetCommit": {"commitId": target}}
        fail_items = True

        def get(route, **query):
            if route == "/pullrequests/123":
                return pr
            if route == "/diffs/commits":
                return {"allChangesIncluded": True, "commonCommit": common,
                        "changes": [{"item": {"path": "/file.md"}, "changeType": "edit"}]}
            if fail_items:
                raise RuntimeError("observed failed item GET")
            return {"content": "actual " + query["versionDescriptor.version"]}

        with patch.object(READER, "ROOT", self.directory), patch.object(READER, "get", side_effect=get):
            with self.assertRaisesRegex(RuntimeError, "failed item GET"):
                READER.start(URL, review_root=self.directory)
            self.assertTrue((self.directory / "snapshot.json").exists())
            self.assertFalse((self.directory / "pr.diff").exists())
            fail_items = False
            with self.assertRaisesRegex(ValueError, "incomplete"):
                READER.start(URL, review_root=self.directory)

    def test_snapshot_uses_actual_pins_and_change_kinds_then_refuses_drift(self):
        source, target, common = "1" * 40, "2" * 40, "3" * 40
        pr = {"pullRequestId": 123, "repository": REPOSITORY,
              "lastMergeSourceCommit": {"commitId": source},
              "lastMergeTargetCommit": {"commitId": target}}
        changes = [{"item": {"path": "/" + path}, "changeType": kind}
                   for path, kind in (("edited.md", "edit"), ("added.md", "add"), ("deleted.md", "delete"))]

        def get(route, **query):
            if route == "/pullrequests/123":
                return pr
            if route == "/diffs/commits":
                self.assertEqual(query["baseVersion"], target)
                self.assertEqual(query["targetVersion"], source)
                return {"allChangesIncluded": True, "commonCommit": common, "changes": changes}
            self.assertEqual(route, "/items")
            return {"content": "actual " + query["versionDescriptor.version"] + "\n"}

        with patch.object(READER, "ROOT", self.directory), patch.object(READER, "get", side_effect=get):
            value = READER.start(URL, review_root=self.directory)
            self.assertEqual((value["source"], value["target"], value["mergeBase"]), (source, target, common))
            self.assertEqual(value["changedFiles"], ["added.md", "deleted.md", "edited.md"])
            self.assertEqual((self.directory / "source" / "deleted.md").read_text(), "")
            self.assertEqual((self.directory / "base" / "added.md").read_text(), "")
            self.assertIn("+actual " + source, (self.directory / "pr.diff").read_text())
            self.assertEqual("Fabrikam", value["review"]["repository"]["project"]["name"])
            self.assertEqual("contoso", value["review"]["organization"])
            pr["lastMergeSourceCommit"]["commitId"] = "4" * 40
            with self.assertRaisesRegex(ValueError, "advanced"):
                READER.metadata()
            with self.assertRaisesRegex(ValueError, "cannot be replaced"):
                READER.prepare()
            with self.assertRaisesRegex(ValueError, "frozen pins"):
                READER.file("edited.md", "5" * 40)

    def test_auth_pending_retries_actual_json_but_never_retries_denials(self):
        (self.directory / "request.json").write_text(json.dumps(READER.parse_pr_url(URL)))
        pending = subprocess.CompletedProcess([], 0, b"auth_pending\n503", b"")
        allowed = subprocess.CompletedProcess([], 0, b'{"pullRequestId":123}\n200', b"")
        denied = subprocess.CompletedProcess([], 0, b"denied\n403", b"")
        with patch.object(READER, "ROOT", self.directory), patch.object(
                READER.subprocess, "run", side_effect=[pending, allowed]) as calls, patch.object(READER.time, "sleep"):
            self.assertEqual(READER.get("/pullrequests/123"), {"pullRequestId": 123})
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(
            "https://dev.azure.com/contoso/Fabrikam/_apis/git/repositories/Widgets/pullrequests/123?api-version=7.1",
            calls.call_args.args[0][-1])
        self.assertNotIn("--user", calls.call_args.args[0])
        statuses = [json.loads(row)["status"]
                    for row in (self.directory / "fetch-attempts.jsonl").read_text().splitlines()]
        self.assertEqual(statuses, [503, 200])
        with patch.object(READER, "ROOT", self.directory), patch.object(
                READER.subprocess, "run", return_value=denied) as calls:
            with self.assertRaisesRegex(RuntimeError, "403"):
                READER.get("/pullrequests/123")
        self.assertEqual(calls.call_count, 1)

    def test_each_url_form_keeps_its_own_api_host(self):
        expected = {"schemaVersion": 4, "pullRequestId": 123, "apiHost": "dev.azure.com",
                    "organization": "contoso", "project": "Fabrikam", "repository": "Widgets"}
        for url in (URL, URL + "/"):
            with self.subTest(url=url):
                self.assertEqual(expected, READER.parse_pr_url(url))
        hosted = {**expected, "apiHost": "contoso.visualstudio.com"}
        for url in ("https://contoso.visualstudio.com/Fabrikam/_git/Widgets/pullrequest/123",
                    f"https://contoso.visualstudio.com/{COLLECTION}/Fabrikam/_git/Widgets/pullrequest/123",
                    f"https://contoso.visualstudio.com/{COLLECTION.lower()}/Fabrikam/_git/Widgets/pullrequest/123"):
            with self.subTest(url=url):
                self.assertEqual(hosted, READER.parse_pr_url(url))
        spaced = READER.parse_pr_url("https://dev.azure.com/contoso/Fabrikam%20Tools/_git/Widget%20App/pullrequest/7")
        self.assertEqual(("Fabrikam Tools", "Widget App", 7),
                         (spaced["project"], spaced["repository"], spaced["pullRequestId"]))
        guids = READER.parse_pr_url(f"https://dev.azure.com/contoso/{PROJECT.upper()}/_git/{REPOSITORY_ID}/pullrequest/9")
        self.assertEqual((PROJECT, REPOSITORY_ID), (guids["project"], guids["repository"]))
        with patch.object(READER, "ROOT", self.directory):
            (self.directory / "request.json").write_text(json.dumps(READER.parse_pr_url(URL)))
            self.assertEqual("https://dev.azure.com/contoso/Fabrikam/_apis/git/repositories/Widgets",
                             READER.repository_prefix())
            # A visualstudio.com URL only yields the lowercased organization, so the API call stays
            # on that host instead of re-casing the organization into a dev.azure.com path.
            (self.directory / "request.json").write_text(json.dumps(READER.parse_pr_url(
                f"https://contoso.visualstudio.com/{COLLECTION}/Fabrikam/_git/Widgets/pullrequest/123")))
            self.assertEqual("https://contoso.visualstudio.com/Fabrikam/_apis/git/repositories/Widgets",
                             READER.repository_prefix())
            (self.directory / "request.json").write_text(json.dumps(spaced))
            self.assertEqual("https://dev.azure.com/contoso/Fabrikam%20Tools/_apis/git/repositories/Widget%20App",
                             READER.repository_prefix())

    def test_malformed_urls_fail_before_any_fetch_or_snapshot_write(self):
        invalid = (
            None,
            "",
            URL.replace("/123", "/0"),
            URL.replace("/123", "/2147483648"),
            URL.replace("/123", "/123;whoami"),
            URL.replace("/123", "/-1"),
            URL.replace("/123", "/\u0661\u0662"),
            URL.replace("/123", "/123/files"),
            URL.replace("pullrequest", "pullrequests"),
            URL.replace("/_git", "/_gitx"),
            URL.replace("/_git", ""),
            URL.replace("dev.azure.com/contoso", "evil.example/contoso"),
            URL.replace("dev.azure.com", "DEV.azure.com"),
            URL.replace("dev.azure.com", "dev.azure.com."),
            URL.replace("https://", "https://user@"),
            URL.replace("https://", "https://user:secret@"),
            URL.replace("https://", "http://"),
            URL.replace("dev.azure.com", "dev.azure.com:8443"),
            URL.replace("dev.azure.com", "dev.azure.com:443"),
            URL + "?redirect=https://evil.example",
            URL + "#fragment",
            URL + "?",
            URL.replace("https://dev.azure.com/contoso/", "https://dev.azure.com/"),
            "https://dev.azure.com/",
            URL.replace("/contoso/", "/con_toso/"),
            URL.replace("/contoso/", "/" + "a" * 51 + "/"),
            URL.replace("/contoso/", "/-contoso/"),
            URL.replace("Widgets", "Nova%2fother"),
            URL.replace("Widgets", "%252e%252e"),
            URL.replace("Widgets", "%2e%2e"),
            URL.replace("Widgets", ".."),
            URL.replace("Widgets", "Nova%5cother"),
            URL.replace("Widgets", "Nova\\other"),
            URL.replace("Widgets", "Nova%00"),
            URL.replace("Widgets", "%FF"),
            URL.replace("Widgets", "Nova%xx"),
            URL.replace("Widgets", "Nova other"),
            URL.replace("/Widgets/", "//"),
            "https://contoso.visualstudio.com.evil.example/Fabrikam/_git/Widgets/pullrequest/123",
            "https://evil.example/contoso.visualstudio.com/Fabrikam/_git/Widgets/pullrequest/123",
            "https://visualstudio.com/Fabrikam/_git/Widgets/pullrequest/123",
            "https://-contoso.visualstudio.com/Fabrikam/_git/Widgets/pullrequest/123",
            "https://a.b.visualstudio.com/Fabrikam/_git/Widgets/pullrequest/123",
            "https://Contoso.visualstudio.com/Fabrikam/_git/Widgets/pullrequest/123",
            "https://contoso.visualstudio.com/Fabrikam/_git/Widgets/pullrequest/123?x=1",
            "https://contoso.visualstudio.com:443/Fabrikam/_git/Widgets/pullrequest/123",
            f"https://contoso.visualstudio.com/{COLLECTION}/{COLLECTION}/Fabrikam/_git/Widgets/pullrequest/123",
            f"https://contoso.visualstudio.com/{COLLECTION}/_git/Widgets/pullrequest/123",
            f"https://dev.azure.com/contoso/{COLLECTION}/Fabrikam/_git/Widgets/pullrequest/123",
        )
        with patch.object(READER, "ROOT", self.directory), patch.object(READER, "get") as calls:
            for url in invalid:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    READER.start(url, review_root=self.directory / "never")
            calls.assert_not_called()
        self.assertEqual([], list(self.directory.iterdir()))

    def test_repositories_keep_verified_identity_and_transport_in_each_frozen_context(self):
        other = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "name": "Gadgets",
                 "project": REPOSITORY["project"]}
        selected = REPOSITORY
        pr = {"pullRequestId": 123, "lastMergeSourceCommit": {"commitId": "1" * 40},
              "lastMergeTargetCommit": {"commitId": "2" * 40}}
        urls = []

        def transport(arguments, **kwargs):
            url = arguments[-1]
            urls.append(url)
            if "/pullrequests/" in url:
                body = {**pr, "repository": selected}
            elif "/diffs/commits" in url:
                body = {"allChangesIncluded": True, "commonCommit": "3" * 40,
                        "changes": [{"item": {"path": "/file.md"}, "changeType": "edit"}]}
            else:
                body = {"content": selected["name"] + "\n"}
            return subprocess.CompletedProcess([], 0, json.dumps(body).encode() + b"\n200", b"")

        with patch.object(READER, "ROOT", self.directory), patch.object(READER.subprocess, "run", side_effect=transport):
            first = READER.start(URL, review_root=self.directory / "widgets")
            original = (self.directory / "widgets" / "snapshot.json").read_bytes()
            split = len(urls)
            selected = other
            second = READER.start(
                f"https://contoso.visualstudio.com/{COLLECTION}/{PROJECT}/_git/{other['id']}/pullrequest/123",
                review_root=self.directory / "gadgets")
            self.assertNotEqual(first["review"]["repository"], second["review"]["repository"])
            self.assertEqual("Fabrikam", second["review"]["repository"]["project"]["name"])
            self.assertTrue(all(url.startswith("https://dev.azure.com/contoso/") for url in urls[:split]))
            self.assertTrue(all(url.startswith("https://contoso.visualstudio.com/") for url in urls[split:]))
            self.assertTrue(any(url.startswith(
                f"https://contoso.visualstudio.com/{PROJECT}/_apis/git/repositories/{other['id']}/items?")
                for url in urls))
            self.assertEqual(original, (self.directory / "widgets" / "snapshot.json").read_bytes())
            request = json.loads((READER.ROOT / "request.json").read_text())
            request["repository"] = "Other"
            (READER.ROOT / "request.json").write_text(json.dumps(request))
            with self.assertRaisesRegex(ValueError, "different repository"):
                READER.completed_snapshot()

    def test_project_or_repository_mismatch_is_rejected_before_snapshot(self):
        cases = (
            ({**REPOSITORY, "name": "Gadgets"}, URL),
            ({**REPOSITORY, "project": {"id": PROJECT, "name": "Other"}}, URL),
            (REPOSITORY, URL.replace("Fabrikam", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")),
            (REPOSITORY, URL.replace("Widgets", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")),
            ({**REPOSITORY, "id": "not-a-guid"}, URL),
            ({**REPOSITORY, "project": {"id": "not-a-guid", "name": "Fabrikam"}}, URL),
            ({"id": REPOSITORY_ID, "name": "Widgets"}, URL),
            (None, URL),
        )
        with patch.object(READER, "ROOT", self.directory), patch.object(READER, "get") as get:
            for repository, url in cases:
                (self.directory / "request.json").write_text(json.dumps(READER.parse_pr_url(url)))
                get.return_value = {"pullRequestId": 123, "repository": repository}
                with self.subTest(url=url, repository=repository), \
                        self.assertRaisesRegex(ValueError, "different repository"):
                    READER.metadata()
                self.assertFalse((self.directory / "snapshot.json").exists())
            for url in (URL.replace("Fabrikam", "fabrikam").replace("Widgets", "WIDGETS"),
                        URL.replace("Fabrikam", PROJECT).replace("Widgets", REPOSITORY_ID.upper())):
                (self.directory / "request.json").write_text(json.dumps(READER.parse_pr_url(url)))
                get.return_value = {"pullRequestId": 123, "repository": REPOSITORY,
                                    "lastMergeSourceCommit": {"commitId": "1" * 40},
                                    "lastMergeTargetCommit": {"commitId": "2" * 40}}
                with self.subTest(matching=url):
                    self.assertEqual("Fabrikam", READER.metadata()["repository"]["project"]["name"])

    def test_existing_root_for_another_repository_is_refused_without_rewriting(self):
        (self.directory / "request.json").write_text(json.dumps(READER.parse_pr_url(URL)))
        original = (self.directory / "request.json").read_bytes()
        with patch.object(READER, "ROOT", self.directory), patch.object(READER, "get") as get:
            for url in (URL.replace("Widgets", "Gadgets"), URL.replace("Fabrikam", "Other"),
                        URL.replace("contoso", "northwind")):
                with self.subTest(url=url), self.assertRaisesRegex(ValueError, "different repository"):
                    READER.start(url, review_root=self.directory)
            get.assert_not_called()
        self.assertEqual(original, (self.directory / "request.json").read_bytes())

    def test_legacy_and_schema_two_requests_are_removed(self):
        self.assertFalse(hasattr(READER, "PREFIX"))
        self.assertFalse(hasattr(READER, "REPOSITORY"))
        current = READER.parse_pr_url(URL)
        for value in ({"pullRequestId": 123},
                      {**current, "schemaVersion": 2},
                      {**current, "schemaVersion": True},
                      {**current, "schemaVersion": 3},
                      {**current, "schemaVersion": "4"},
                      {key: item for key, item in current.items() if key != "apiHost"},
                      {**current, "apiHost": "evil.example"},
                      {**current, "apiHost": "northwind.visualstudio.com"},
                      {key: item for key, item in current.items() if key != "organization"},
                      {**current, "extra": 1},
                      {**current, "pullRequestId": True},
                      {**current, "pullRequestId": 0},
                      {**current, "organization": "con_toso"},
                      {**current, "project": "a/b"},
                      {**current, "repository": ""},
                      [123]):
            (self.directory / "request.json").write_text(json.dumps(value))
            with self.subTest(value=value), patch.object(READER, "ROOT", self.directory), \
                    patch.object(READER, "get") as get:
                with self.assertRaises(ValueError):
                    READER.request()
                with self.assertRaises(ValueError):
                    READER.start(URL, review_root=self.directory)
                get.assert_not_called()


if __name__ == "__main__":
    unittest.main()

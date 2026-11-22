"""sandbox_fetch.py contracts with a fake curl. No network.

Organizations, projects and repositories below are fictional.
"""
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "sandbox-http" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import sandbox_fetch  # noqa: E402

ADO = "https://dev.azure.com/contoso/Fabrikam/_apis/git/repositories/Widgets/pullrequests/7/threads?api-version=7.1"


class FakeCurl:
    def __init__(self, body=b"{}", code=b"200", returncode=0):
        self.body, self.code, self.returncode = body, code, returncode
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, self.returncode, self.body + b"\n" + self.code, b"curl noise")


class SandboxFetchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".logs" / "sandbox-fetch" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)

    def run_main(self, argv, curl):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sandbox_fetch.subprocess, "run", side_effect=curl), \
                patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            code = sandbox_fetch.main(argv)
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_get_returns_parsed_json_and_safe_curl_command(self):
        curl = FakeCurl(b'{"value": [1]}')
        code, out, err = self.run_main(["--url", ADO, "--method", "GET"], curl)
        self.assertEqual((0, "ok", 200, {"value": [1]}), (code, out["status"], out["http_status"], out["body"]))
        command, kwargs = curl.calls[0]
        self.assertEqual(["curl", "-q"], command[:2])
        self.assertNotIn("-L", command)
        self.assertNotIn("--location", command)
        self.assertEqual(ADO, command[-1])
        self.assertIn("--max-time", command)
        self.assertEqual(b"", kwargs["input"])
        self.assertEqual("sandbox_fetch: HTTP 200 (ok)\n", err)

    def test_post_sends_file_body_on_stdin_with_headers(self):
        body = self.root / "body.json"
        body.write_bytes(b'{"comments": []}')
        curl = FakeCurl(b'{"id": 5}', b"201")
        code, out, err = self.run_main(
            ["--url", "https://api.github.com/repos/octo/widgets/issues/3/comments", "--method", "POST",
             "--header", "Content-Type:application/json", "--header", "Accept:application/vnd.github+json",
             "--data-file", str(body)], curl)
        self.assertEqual((0, {"id": 5}), (code, out["body"]))
        command, kwargs = curl.calls[0]
        self.assertEqual(b'{"comments": []}', kwargs["input"])
        self.assertIn("Content-Type: application/json", command)
        self.assertIn("Accept: application/vnd.github+json", command)
        self.assertEqual("@-", command[command.index("--data-binary") + 1])
        self.assertNotIn("comments", err)

    def test_credentials_unsafe_urls_and_hosts_are_refused_before_curl(self):
        cases = [
            ["--url", ADO, "--header", "Authorization:Bearer abc"],
            ["--url", ADO, "--header", "cookie: a=b"],
            ["--url", ADO, "--header", "Proxy-Authorization:Basic x"],
            ["--url", ADO.replace("https", "http", 1)],
            ["--url", "https://example.com/x"],
            ["--url", "https://dev.azure.com.example.com/x"],
            ["--url", "https://user:pw@dev.azure.com/x"],
            ["--url", "https://dev.azure.com:8443/x"],
            ["--url", "https://visualstudio.com/x"],
            ["--url", ADO, "--header", "NoColon"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                curl = FakeCurl()
                code, out, err = self.run_main(argv, curl)
                self.assertEqual((2, "refused", []), (code, out["status"], curl.calls))
                self.assertNotIn("abc", err + json.dumps(out))

    def test_visualstudio_subdomain_is_allowed(self):
        code, out, _ = self.run_main(["--url", "https://contoso.visualstudio.com/Fabrikam/_apis/projects"], FakeCurl())
        self.assertEqual((0, "ok"), (code, out["status"]))

    def test_get_with_body_is_refused(self):
        body = self.root / "body.json"
        body.write_text("{}")
        curl = FakeCurl()
        code, out, _ = self.run_main(["--url", ADO, "--data-file", str(body)], curl)
        self.assertEqual((2, []), (code, curl.calls))

    def test_proxy_denial_auth_pending_http_error_and_transport_failure(self):
        cases = [
            (FakeCurl(b'{"error": "denied", "reason": "policy"}', b"403"), 10, "denied", 403),
            (FakeCurl(b'{"status": "auth_pending"}', b"511"), 11, "auth_pending", 511),
            (FakeCurl(b'{"message": "TF401019"}', b"403"), 1, "http_error", 403),
            (FakeCurl(b"not found", b"404"), 1, "http_error", 404),
            (FakeCurl(b"", b"000", returncode=28), 12, "transport_error", None),
        ]
        for curl, exit_code, status, http_status in cases:
            with self.subTest(status=status, http_status=http_status):
                code, out, err = self.run_main(["--url", ADO], curl)
                self.assertEqual((exit_code, status, http_status), (code, out["status"], out["http_status"]))
                self.assertNotIn("policy", err)
                self.assertNotIn("TF401019", err)

    def test_output_file_receives_raw_body(self):
        target = self.root / "out.json"
        code, out, _ = self.run_main(["--url", ADO, "--output", str(target)], FakeCurl(b'{"a": 1}'))
        self.assertEqual(0, code)
        self.assertNotIn("body", out)
        self.assertEqual(b'{"a": 1}', target.read_bytes())


if __name__ == "__main__":
    unittest.main()

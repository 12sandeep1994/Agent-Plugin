#!/usr/bin/env python3
"""Make one HTTPS request to Azure DevOps or GitHub through the sandbox egress proxy.

The sandbox egress proxy adds credentials outside the sandbox. This helper never
accepts or sends credentials: Authorization, Proxy-Authorization and Cookie
headers are refused. Only https URLs on an allow-listed host are accepted.

Usage:
    sandbox_fetch.py --url URL [--method GET|POST|PATCH|PUT]
                     [--header Name:Value ...] [--data-file PATH|-] [--output PATH]
                     [--timeout SECONDS]

stdout is exactly one JSON object:
    {"status": "...", "http_status": 200, "method": "GET", "url": "...", "body": ...}
"body" is parsed JSON when the response is JSON, else text, else null. With
--output the raw body is written to that file and "body_file" replaces "body".

Exit codes:
    0  ok               HTTP 2xx
    1  http_error       any other HTTP status
    2  refused          invalid or unsafe arguments; nothing was sent
    10 denied           the egress proxy denied the request (HTTP 403 {"error": "denied"})
    11 auth_pending     the egress proxy is still acquiring credentials (HTTP 511)
    12 transport_error  curl failed or timed out; the outcome of a write is unknown

stderr carries at most one short line (HTTP status or result kind). Headers and
bodies are never written to stderr.

Python 3.10+ standard library only; shells out to curl.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_HOSTS = ("dev.azure.com", "api.github.com", "github.com")
ALLOWED_HOST_SUFFIXES = (".visualstudio.com",)
METHODS = ("GET", "POST", "PATCH", "PUT")
FORBIDDEN_HEADERS = {"authorization", "proxy-authorization", "cookie"}
HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
CONNECT_TIMEOUT = 30

EXIT = {"ok": 0, "http_error": 1, "refused": 2, "denied": 10,
        "auth_pending": 11, "transport_error": 12}


class Refused(ValueError):
    pass


def check_url(url: str) -> str:
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        raise Refused("URL contains whitespace or control characters.")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise Refused("Only https URLs are allowed.")
    if "@" in parts.netloc:
        raise Refused("URLs with user info are not allowed.")
    try:
        port = parts.port
    except ValueError as error:
        raise Refused("URL port is invalid.") from error
    if port not in (None, 443):
        raise Refused("Only the default https port is allowed.")
    if parts.fragment:
        raise Refused("URL fragments are not allowed.")
    host = (parts.hostname or "").lower()
    if not (host in ALLOWED_HOSTS
            or any(host.endswith(suffix) and len(host) > len(suffix) for suffix in ALLOWED_HOST_SUFFIXES)):
        raise Refused("Host is not allowed. Allowed: dev.azure.com, *.visualstudio.com, "
                      "api.github.com, github.com.")
    return url


def check_header(raw: str) -> str:
    name, separator, value = raw.partition(":")
    name, value = name.strip(), value.strip()
    if not separator or not HEADER_NAME.fullmatch(name):
        raise Refused("Headers must look like Name:Value.")
    if any(c in value for c in "\r\n\0"):
        raise Refused("Header values cannot contain line breaks.")
    if name.lower() in FORBIDDEN_HEADERS:
        raise Refused(f"The {name} header is not allowed; the egress proxy adds credentials.")
    return f"{name}: {value}"


def read_data(path: str | None) -> bytes | None:
    if path is None:
        return None
    if path == "-":
        data = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    else:
        file = Path(path)
        if file.stat().st_size > MAX_REQUEST_BYTES:
            raise Refused("Request body exceeds 16 MiB.")
        data = file.read_bytes()
    if len(data) > MAX_REQUEST_BYTES:
        raise Refused("Request body exceeds 16 MiB.")
    return data


def curl_command(url: str, method: str, headers: list[str], has_data: bool, timeout: int) -> list[str]:
    # -q must be the first argument: it stops curl from reading a ~/.curlrc that could add
    # headers, credentials or redirects. The proxy comes from the sandbox environment.
    command = ["curl", "-q", "--silent", "--show-error", "--proto", "=https",
               "--max-redirs", "0", "--connect-timeout", str(CONNECT_TIMEOUT),
               "--max-time", str(timeout), "--max-filesize", str(MAX_RESPONSE_BYTES),
               "--request", method]
    for header in headers:
        command += ["--header", header]
    if has_data:
        command += ["--data-binary", "@-"]
    command += ["--write-out", "\n%{http_code}", url]
    return command


def classify(code: int, body: bytes) -> str:
    if 200 <= code < 300:
        return "ok"
    if code == 511:
        return "auth_pending"
    if code == 403:
        try:
            if json.loads(body).get("error") == "denied":
                return "denied"
        except (ValueError, AttributeError):
            pass
    return "http_error"


def decode_body(body: bytes):
    text = body.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def fetch(url: str, method: str = "GET", headers: list[str] | None = None,
          data_file: str | None = None, output: str | None = None,
          timeout: int = DEFAULT_TIMEOUT) -> dict:
    method = method.upper()
    if method not in METHODS:
        raise Refused(f"Method must be one of {', '.join(METHODS)}.")
    if not 1 <= timeout <= MAX_TIMEOUT:
        raise Refused(f"Timeout must be between 1 and {MAX_TIMEOUT} seconds.")
    url = check_url(url)
    checked = [check_header(header) for header in headers or []]
    data = read_data(data_file)
    if data is not None and method == "GET":
        raise Refused("GET requests cannot send a body.")
    request = {"method": method, "url": url}
    try:
        reply = subprocess.run(curl_command(url, method, checked, data is not None, timeout),
                               input=data if data is not None else b"", capture_output=True,
                               timeout=timeout + CONNECT_TIMEOUT + 5)
    except subprocess.TimeoutExpired:
        return {"status": "transport_error", "http_status": None, **request,
                "error": "curl did not finish in time"}
    except OSError:
        return {"status": "transport_error", "http_status": None, **request,
                "error": "curl could not be started"}
    body, separator, code = reply.stdout.rpartition(b"\n")
    if reply.returncode or not separator or not code.isdigit() or code == b"000":
        return {"status": "transport_error", "http_status": None, **request,
                "error": f"curl exit {reply.returncode}"}
    http_status = int(code)
    result = {"status": classify(http_status, body), "http_status": http_status, **request}
    if output:
        try:
            Path(output).write_bytes(body)
            result["body_file"] = str(Path(output).resolve())
            return result
        except OSError as error:
            # The request was already sent, so report its real outcome rather than "refused".
            result["output_error"] = error.strerror or "I/O error"
    result["body"] = decode_body(body)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--method", default="GET", type=str.upper, choices=METHODS)
    parser.add_argument("--header", action="append", default=[], metavar="Name:Value")
    parser.add_argument("--data-file", help="Request body file, or - for stdin.")
    parser.add_argument("--output", help="Write the raw response body to this file.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    try:
        result = fetch(args.url, args.method, args.header, args.data_file, args.output, args.timeout)
    except Refused as error:
        result = {"status": "refused", "http_status": None, "error": str(error)}
    except OSError as error:
        result = {"status": "refused", "http_status": None,
                  "error": f"Local file error: {error.strerror or 'I/O error'}"}
    print(json.dumps(result, indent=2))
    if result["http_status"] is not None:
        print(f"sandbox_fetch: HTTP {result['http_status']} ({result['status']})", file=sys.stderr)
    else:
        print(f"sandbox_fetch: {result['status']}", file=sys.stderr)
    return EXIT[result["status"]]


if __name__ == "__main__":
    sys.exit(main())

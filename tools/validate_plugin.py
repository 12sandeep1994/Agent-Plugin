#!/usr/bin/env python3
"""Validate a plugin directory against the NCS SandboxTools gateway parser.

Mirrors ``crates/mcp-gateway/src/public_plugins.rs`` (``PluginBundle::from_files``,
``selected_files``, ``discover_instructions``, ``instructions``,
``validate_plugin_path``), ``skills.rs`` (``parse_instruction_frontmatter``) and
``resources.rs`` (``validate_id``, ``validate_prefix``).

Usage::

    python tools/validate_plugin.py code-reviewer [--source index|fs] [--version X]

``--source index`` (default inside a git work tree) reads the staged blobs and
modes, which is what GitHub serves for a commit. ``--source fs`` walks the
working directory instead.

YAML: uses PyYAML when installed; otherwise a strict subset parser that raises
on any construct it does not understand rather than guessing.

Python 3 standard library only. Exit code 0 means the gateway would accept the
bundle; any violation raises and exits 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import unicodedata

MAX_FILES = 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_DESCRIPTION_BYTES = 16 * 1024
MANIFEST = ".claude-plugin/plugin.json"


class PluginError(Exception):
    pass


# --- resources.rs ---------------------------------------------------------

def validate_id(value: str, field: str) -> None:
    if (not value or len(value.encode("utf-8")) > 64
            or not all(c.isascii() and (c.isalnum() or c in "-_") for c in value)):
        raise PluginError(f"{field}: expected 1-64 ASCII letters, digits, '-' or '_' (got {value!r})")


def _rust_trim(s: str) -> str:
    # Rust str::trim removes Unicode White_Space; Python's isspace is equivalent
    # for the characters that can reach here (controls are rejected earlier).
    return s.strip()


def validate_prefix(value: str, field: str, allow_empty: bool) -> None:
    if value == "" and allow_empty:
        return
    if (value == "" or len(value.encode("utf-8")) > 512
            or any(c in value for c in "\\:%")
            or any(unicodedata.category(c) == "Cc" for c in value)
            or any(p in ("", ".", "..") or _rust_trim(p) != p for p in value.split("/"))):
        raise PluginError(f"{field}: expected a bounded relative storage prefix (got {value!r})")


# --- public_plugins.rs ----------------------------------------------------

def validate_plugin_path(path: str) -> None:
    validate_prefix(path, "plugin file", False)
    if any(p.lower() == ".git" or p == ".gitmodules" for p in path.split("/")):
        raise PluginError(f"Git metadata and submodules are not supported in a plugin bundle: {path}")


def component_paths(manifest: dict, key: str):
    if key not in manifest:
        return None
    value = manifest[key]
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list) and len(value) <= MAX_FILES:
        values = value
    else:
        raise PluginError(f"Plugin {key} must be a path or bounded list of paths")
    paths = set()
    for v in values:
        if not isinstance(v, str):
            raise PluginError("Component path must be a string")
        if v == "":
            raise PluginError("Component path cannot be empty")
        if v.startswith("./"):
            v = v[2:]
        if v.endswith("/"):
            v = v[:-1]
        if v == ".":
            v = ""
        validate_prefix(v, "plugin component path", True)
        paths.add(v)
    return sorted(paths)


def skill_paths(files: dict, directory: str) -> set:
    prefix = f"{directory}/" if directory else ""
    direct = f"{prefix}SKILL.md"
    if direct in files:
        return {direct}
    out = set()
    for path in files:
        if path.startswith(prefix) and path.endswith("/SKILL.md"):
            name = path[len(prefix):-len("/SKILL.md")]
            if name and "/" not in name:
                out.add(path)
    return out


def instructions(paths, files: dict, skills: bool) -> list:
    names = set()
    result = []
    for path in sorted(paths):  # BTreeSet order
        if path not in files:
            raise PluginError(f"Declared instruction file is absent: {path}")
        try:
            content = files[path]["bytes"].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PluginError(f"Instruction must be UTF-8: {path}") from exc
        try:
            yaml, _body = parse_instruction_frontmatter(content)
        except PluginError as exc:
            raise PluginError(f"Invalid instruction at {path}: {exc}") from exc
        if skills:
            fallback = path[:-len("/SKILL.md")].rsplit("/", 1)[-1] if path.endswith("/SKILL.md") else None
        else:
            last = path.rsplit("/", 1)[-1]
            fallback = last.rsplit(".", 1)[0] if "." in last else None
        if "name" not in yaml:
            if fallback is None:
                raise PluginError(f"Root SKILL.md requires an explicit name: {path}")
            name = fallback
        else:
            name = yaml["name"]
            if not isinstance(name, str):
                raise PluginError(f"Instruction name must be a string: {path}")
        validate_id(name, f"instruction.name ({path})")
        if name in names:
            raise PluginError(f"Duplicate instruction name: {name}")
        names.add(name)
        description = yaml.get("description", "")
        if not isinstance(description, str):
            raise PluginError(f"Instruction description must be a string: {path}")
        if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
            raise PluginError(f"Instruction description exceeds 16384 bytes: {path}")
        result.append({"name": name, "description": description, "path": path})
    return result


def discover_instructions(manifest: dict, files: dict) -> dict:
    skills = skill_paths(files, "skills")
    dirs = component_paths(manifest, "skills")
    if dirs is not None:
        for d in dirs:
            selected = skill_paths(files, d)
            if not selected:
                raise PluginError(f"Declared skill path contains no SKILL.md: {d!r}")
            skills |= selected
    agent_paths = component_paths(manifest, "agents")
    if agent_paths is not None:
        for p in agent_paths:
            if not p.lower().endswith(".md") or p not in files:
                raise PluginError(f"Declared agent must identify an existing Markdown file: {p!r}")
        agents = set(agent_paths)
    else:
        agents = {p for p in files
                  if p.startswith("agents/") and "/" not in p[len("agents/"):]
                  and p.lower().endswith(".md")}
    return {"skills": instructions(skills, files, True),
            "agents": instructions(agents, files, False)}


def from_files(files: dict, version=None) -> dict:
    if not files or len(files) > MAX_FILES:
        raise PluginError(f"Plugin bundle exceeds file count limits ({len(files)} files)")
    total = 0
    for path, f in sorted(files.items()):
        validate_plugin_path(path)
        if f["mode"] not in ("100644", "100755"):
            raise PluginError(f"Symlinks, submodules and special files are not supported: {path} ({f['mode']})")
        size = len(f["bytes"])
        if size > MAX_FILE_BYTES:
            raise PluginError(f"Plugin file exceeds size limit: {path}")
        total += size
        if total > MAX_BUNDLE_BYTES:
            raise PluginError("Plugin bundle exceeds size limit")
        ancestor = path
        while "/" in ancestor:
            ancestor = ancestor.rsplit("/", 1)[0]
            if ancestor in files:
                raise PluginError(f"Plugin bundle aliases a file and directory: {ancestor}")
    if MANIFEST not in files:
        raise PluginError("Selected plugin has no .claude-plugin/plugin.json")
    try:
        manifest = json.loads(files[MANIFEST]["bytes"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PluginError(f"Invalid plugin manifest: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str):
        raise PluginError("Plugin manifest requires a name")
    validate_id(manifest["name"], "plugin.name")
    if version is not None and manifest.get("version") != version:
        raise PluginError("Plugin manifest does not match requested exact version")
    discovery = discover_instructions(manifest, files)
    digest = hashlib.sha256()
    for path, f in sorted(files.items(), key=lambda kv: kv[0].encode("utf-8")):
        p = path.encode("utf-8")
        digest.update(struct.pack(">Q", len(p)))
        digest.update(p)
        digest.update(bytes([1 if f["mode"] == "100755" else 0]))
        digest.update(struct.pack(">Q", len(f["bytes"])))
        digest.update(f["bytes"])
    return {"manifest": manifest, "files": len(files), "bytes": total,
            "digest": digest.hexdigest(), **discovery}


# --- skills.rs: parse_instruction_frontmatter -----------------------------

def rust_lines(content: str) -> list:
    # str::lines: split on '\n', strip one trailing '\r', no final empty line.
    parts = content.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def parse_instruction_frontmatter(content: str):
    lines = rust_lines(content)
    if not lines or lines[0] != "---":
        raise PluginError("SKILL.md must start with ---")
    try:
        end = lines[1:].index("---") + 1
    except ValueError as exc:
        raise PluginError("No closing --- found for YAML frontmatter") from exc
    frontmatter = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:])
    yaml = load_yaml(frontmatter)
    if not isinstance(yaml, dict):
        raise PluginError("Instruction frontmatter must be a YAML object")
    return yaml, body


def load_yaml(text: str):
    try:
        import yaml as pyyaml  # type: ignore
    except ImportError:
        return SubsetYaml(text).parse()
    try:
        return pyyaml.safe_load(text)
    except pyyaml.YAMLError as exc:
        raise PluginError(f"Invalid YAML frontmatter: {exc}") from exc


class SubsetYaml:
    """Strict parser for the YAML subset used by plugin frontmatter.

    Supports a top-level mapping of ``key: value`` where value is a plain
    (possibly multi-line), single- or double-quoted scalar, a ``|``/``>``
    block scalar, an empty value, or a block sequence of such scalars.
    Anything else raises so the validator never silently accepts what
    serde_yaml might reject.
    """

    KEY = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):(?: (.*)|)$")
    INDICATORS = set("[]{}#&*!|>'\"%@`,?:-")

    def __init__(self, text: str):
        self.lines = text.split("\n") if text else []
        self.i = 0

    def fail(self, msg: str):
        raise PluginError(f"Invalid YAML frontmatter (line {self.i + 1}): {msg}")

    def parse(self):
        if all(self._blank_or_comment(l) for l in self.lines):
            return None  # serde_yaml: empty document is null -> not a mapping
        out = {}
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if "\t" in line[:len(line) - len(line.lstrip())]:
                self.fail("tab indentation")
            if self._blank_or_comment(line):
                self.i += 1
                continue
            m = self.KEY.match(line)
            if not m:
                self.fail(f"unsupported construct: {line[:40]!r}")
            key, rest = m.group(1), (m.group(2) or "")
            if key in out:
                self.fail(f"duplicate key {key!r}")
            self.i += 1
            out[key] = self._value(rest.strip())
        return out

    @staticmethod
    def _blank_or_comment(line: str) -> bool:
        s = line.strip()
        return s == "" or s.startswith("#")

    def _indented_block(self):
        """Collect following lines that are blank or indented (> column 0)."""
        start = self.i
        while self.i < len(self.lines):
            l = self.lines[self.i]
            if l.strip() == "" or l.startswith((" ", "\t")):
                self.i += 1
            else:
                break
        block = self.lines[start:self.i]
        while block and block[-1].strip() == "":
            block.pop()
            self.i -= 1
        return block

    def _value(self, rest: str):
        if rest.startswith("#"):
            rest = ""
        if rest == "":
            block = self._indented_block()
            content = [l for l in block if not self._blank_or_comment(l)]
            if not content:
                return None
            if all(l.lstrip().startswith("- ") or l.strip() == "-" for l in content):
                return [self._scalar(l.lstrip()[1:].strip()) for l in content]
            self.fail("nested mappings / mixed blocks are not supported by the subset parser")
        if rest[0] in "|>":
            return self._block_scalar(rest)
        block = self._indented_block()
        if rest[0] in "'\"":
            if block:
                self.fail("multi-line quoted scalars are not supported")
            return self._quoted(rest)
        if block:
            if any(l.strip() == "" for l in block):
                self.fail("blank lines inside multi-line plain scalars are not supported")
            pieces = [rest] + [l.strip() for l in block]
            for p in pieces:
                self._check_plain(p)
            return " ".join(self._strip_comment(p) for p in pieces)
        return self._scalar(rest)

    def _block_scalar(self, header: str):
        m = re.fullmatch(r"([|>])([+-]?)(?:\s+#.*)?", header)
        if not m:
            self.fail(f"unsupported block scalar header {header!r}")
        style, chomp = m.group(1), m.group(2)
        start = self.i
        while self.i < len(self.lines):
            l = self.lines[self.i]
            if l.strip() == "" or l.startswith(" "):
                self.i += 1
            else:
                break
        raw = self.lines[start:self.i]
        nonblank = [l for l in raw if l.strip()]
        if not nonblank:
            return ""
        indent = len(nonblank[0]) - len(nonblank[0].lstrip(" "))
        lines = []
        for l in raw:
            if l.strip() == "":
                lines.append("")
            elif len(l) - len(l.lstrip(" ")) < indent:
                self.fail("block scalar line is less indented than its first line")
            else:
                lines.append(l[indent:])
        trailing = 0
        while lines and lines[-1] == "":
            lines.pop()
            trailing += 1
        if style == "|":
            text = "\n".join(lines)
        else:
            if any(l.startswith((" ", "\t")) for l in lines):
                self.fail("more-indented lines in folded scalars are not supported")
            text, prev_blank = "", False
            for idx, l in enumerate(lines):
                if l == "":
                    text += "\n"
                    prev_blank = True
                    continue
                if idx > 0 and not prev_blank:
                    text += " "
                text += l
                prev_blank = False
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" + "\n" * trailing
        return text + "\n"

    def _quoted(self, s: str):
        q = s[0]
        if q == "'":
            m = re.fullmatch(r"'((?:[^']|'')*)'(\s+#.*)?\s*", s)
            if not m:
                self.fail("malformed single-quoted scalar")
            return m.group(1).replace("''", "'")
        m = re.fullmatch(r'"((?:[^"\\]|\\.)*)"(\s+#.*)?\s*', s)
        if not m:
            self.fail("malformed double-quoted scalar")
        escapes = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0", " ": " "}
        out, it = [], iter(m.group(1))
        for c in it:
            if c == "\\":
                e = next(it)
                if e not in escapes:
                    self.fail(f"unsupported escape \\{e}")
                out.append(escapes[e])
            else:
                out.append(c)
        return "".join(out)

    @staticmethod
    def _strip_comment(s: str) -> str:
        idx = s.find(" #")
        return s[:idx].rstrip() if idx >= 0 else s

    def _check_plain(self, s: str):
        if s and s[0] in self.INDICATORS and not (s[0] in "-?:" and len(s) > 1 and s[1] != " "):
            self.fail(f"plain scalar starts with indicator: {s[:20]!r}")
        body = self._strip_comment(s)
        if ": " in body or body.endswith(":"):
            self.fail(f"plain scalar contains ': ' (mapping indicator): {s[:40]!r}")

    def _scalar(self, s: str):
        if s == "":
            return None
        if s[0] in "'\"":
            return self._quoted(s)
        self._check_plain(s)
        s = self._strip_comment(s)
        if s in ("~", "null", "Null", "NULL"):
            return None
        if s in ("true", "True", "TRUE"):
            return True
        if s in ("false", "False", "FALSE"):
            return False
        if re.fullmatch(r"[-+]?(0|[1-9][0-9]*)", s):
            return int(s)
        if re.fullmatch(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?", s):
            return float(s)
        return s


# --- file acquisition -----------------------------------------------------

def files_from_index(root: str) -> dict:
    top = subprocess.run(["git", "-C", root, "rev-parse", "--show-prefix"],
                         check=True, capture_output=True, text=True).stdout.strip()
    listing = subprocess.run(["git", "-C", root, "ls-files", "-s", "-z", "--", "."],
                             check=True, capture_output=True).stdout
    entries = []
    for rec in listing.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, sha, stage = meta.decode().split()
        if stage != "0":
            raise PluginError(f"unmerged index entry: {path!r}")
        rel = path.decode("utf-8")
        if top and rel.startswith(top):
            rel = rel[len(top):]
        entries.append((rel, mode, sha))
    proc = subprocess.run(["git", "-C", root, "cat-file", "--batch"],
                          input="".join(f"{sha}\n" for _, _, sha in entries).encode(),
                          check=True, capture_output=True)
    data, pos, files = proc.stdout, 0, {}
    for rel, mode, sha in entries:
        nl = data.index(b"\n", pos)
        _, kind, size = data[pos:nl].decode().split()
        if kind != "blob":
            raise PluginError(f"{rel}: not a blob ({kind})")
        size = int(size)
        files[rel] = {"mode": mode, "bytes": data[nl + 1:nl + 1 + size]}
        pos = nl + 1 + size + 1
    return files


def files_from_fs(root: str) -> dict:
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full):
                mode = "120000"
            else:
                mode = "100755" if os.access(full, os.X_OK) and os.name != "nt" else "100644"
            with open(full, "rb") as fh:
                files[rel] = {"mode": mode, "bytes": fh.read()}
    return files


def in_git_worktree(root: str) -> bool:
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
                             capture_output=True, text=True)
    except OSError:
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("plugin_dir")
    ap.add_argument("--source", choices=("index", "fs"))
    ap.add_argument("--version", help="exact manifest version the App registration will require")
    args = ap.parse_args(argv)
    source = args.source or ("index" if in_git_worktree(args.plugin_dir) else "fs")
    try:
        files = files_from_index(args.plugin_dir) if source == "index" else files_from_fs(args.plugin_dir)
        result = from_files(files, args.version)
    except PluginError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    m = result["manifest"]
    print(f"OK plugin={m['name']} version={m.get('version', '-')} source={source}")
    print(f"files={result['files']} bytes={result['bytes']} digest={result['digest']}")
    print(f"skills ({len(result['skills'])}): " + ", ".join(s["name"] for s in result["skills"]))
    print(f"agents ({len(result['agents'])}): " + ", ".join(a["name"] for a in result["agents"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

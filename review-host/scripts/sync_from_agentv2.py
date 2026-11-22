#!/usr/bin/env python3
"""Re-sync the verbatim review-host-kit skills from an AgentV2 checkout.

Copies review-setup, repo-setup and merge-review from
``samples/CodeReviewDaemon.Sample/review-host-kit/skills/`` at a committed ref
(default HEAD) of the given AgentV2 checkout into this plugin's ``skills/``,
removes files that no longer exist upstream, and records the source commit in
README.md. Content is read from Git objects, so local uncommitted edits and
platform line-ending conversion never leak into the plugin. Running it twice
with the same ref changes nothing.

Usage: python scripts/sync_from_agentv2.py <agentv2-checkout> [--ref REF] [--check]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

KIT_SKILLS = "samples/CodeReviewDaemon.Sample/review-host-kit/skills"
SKILLS = ("review-setup", "repo-setup", "merge-review")
EXCLUDED_PARTS = {"__pycache__"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
README_MARKER = re.compile(
    r"(<!-- sync:source-start -->).*?(<!-- sync:source-end -->)", re.DOTALL
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _git(checkout: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def _excluded(relative: str) -> bool:
    parts = relative.split("/")
    return bool(EXCLUDED_PARTS.intersection(parts)) or Path(relative).suffix in EXCLUDED_SUFFIXES


def _source_files(checkout: Path, ref: str) -> dict[str, str]:
    """Return {path relative to skills/: blob id} for the synced skills at ref."""
    paths = [f"{KIT_SKILLS}/{name}" for name in SKILLS]
    listing = _git(checkout, "ls-tree", "-r", "-z", "--full-tree", ref, "--", *paths)
    files: dict[str, str] = {}
    for entry in filter(None, listing.split(b"\0")):
        meta, _, path = entry.decode("utf-8").partition("\t")
        mode, kind, blob = meta.split()
        if kind != "blob" or mode == "120000":
            raise SystemExit(f"unsupported entry {path} ({mode} {kind})")
        relative = path[len(KIT_SKILLS) + 1 :]
        if not _excluded(relative):
            files[relative] = blob
    for name in SKILLS:
        if not any(rel.startswith(f"{name}/") for rel in files):
            raise SystemExit(f"{KIT_SKILLS}/{name} not found at {ref}")
    return files


def _target_files() -> set[str]:
    found: set[str] = set()
    for name in SKILLS:
        base = PLUGIN_ROOT / "skills" / name
        if base.exists():
            for path in base.rglob("*"):
                relative = path.relative_to(PLUGIN_ROOT / "skills").as_posix()
                if path.is_file() and not _excluded(relative):
                    found.add(relative)
    return found


def _update_readme(commit: str, write: bool) -> bool:
    readme = PLUGIN_ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    line = (
        f"Source: AgentV2 `{KIT_SKILLS}/` at commit `{commit}`."
    )
    updated, count = README_MARKER.subn(lambda m: f"{m.group(1)}\n{line}\n{m.group(2)}", text)
    if count != 1:
        raise SystemExit("README.md must contain exactly one sync:source marker pair")
    if updated == text:
        return False
    if write:
        readme.write_text(updated, encoding="utf-8", newline="\n")
    return True


def sync(checkout: Path, ref: str, write: bool) -> list[str]:
    commit_ref = _git(checkout, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    source = _source_files(checkout, commit_ref)
    kit_commit = (
        _git(
            checkout,
            "log",
            "-1",
            "--format=%H",
            commit_ref,
            "--",
            *(f"{KIT_SKILLS}/{name}" for name in SKILLS),
        )
        .decode()
        .strip()
    )
    changes: list[str] = []
    for relative, blob in sorted(source.items()):
        content = _git(checkout, "cat-file", "blob", blob)
        target = PLUGIN_ROOT / "skills" / relative
        if target.is_file() and target.read_bytes() == content:
            continue
        changes.append(f"{'update' if target.exists() else 'add'} skills/{relative}")
        if write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    for relative in sorted(_target_files() - source.keys()):
        changes.append(f"remove skills/{relative}")
        if write:
            (PLUGIN_ROOT / "skills" / relative).unlink()
    if write:
        for name in SKILLS:
            base = PLUGIN_ROOT / "skills" / name
            for cache in list(base.rglob("__pycache__")):
                shutil.rmtree(cache)
            for directory in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
    if _update_readme(kit_commit, write):
        changes.append(f"README.md source commit -> {kit_commit}")
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checkout", type=Path, help="Path to an AgentV2 (LmDotnetTools) checkout.")
    parser.add_argument("--ref", default="HEAD", help="Commit-ish to copy from (default HEAD).")
    parser.add_argument(
        "--check", action="store_true", help="Report drift without writing; exit 1 if out of sync."
    )
    args = parser.parse_args(argv)
    checkout = args.checkout.resolve()
    if _git(checkout, "status", "--porcelain", "--", KIT_SKILLS).strip():
        print(
            f"warning: {KIT_SKILLS} has uncommitted changes; they are ignored (copying {args.ref}).",
            file=sys.stderr,
        )
    changes = sync(checkout, args.ref, write=not args.check)
    for change in changes:
        print(change)
    if not changes:
        print("in sync")
    return 1 if args.check and changes else 0


if __name__ == "__main__":
    raise SystemExit(main())

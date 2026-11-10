#!/usr/bin/env python3
"""Decide whether an upstream pull request may be merged into `main`.

The auto-merge merges every open upstream pull request without review. This
script is the only judgement applied to that code before it lands, so it is
deliberately conservative about the things an attacker would change first:

* the machinery that runs during install, build, test or publish
  (``.github/``, ``Makefile``, ``setup.py``, build backend, conftest hooks),
* anything that moves a dependency source: ``git+`` sources, extra index
  URLs, directly downloaded archives, lock files,
* changes too large to have been read by anyone.

Verdicts:

``ok``      merge it.
``review``  keep it out of ``main`` and show it in the run summary; an operator
            can add the pull request to ``allow`` in the policy.
``block``   same, but the finding is a security one and is reported as an
            error rather than a warning.

The policy is read from the trusted base ref (``--policy-ref``), so a pull
request cannot loosen the rules that judge it.

Exit codes: 0 ok, 10 review, 11 block, 2 configuration error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from _gitutil import GitError, added_lines, changed_paths, git, repo_root

POLICY_PATH = ".github/upstream-merge-policy.json"

EXIT_OK = 0
EXIT_REVIEW = 10
EXIT_BLOCK = 11


@dataclass
class Reason:
    kind: str
    message: str

    def render(self) -> str:
        return f"[{self.kind}] {self.message}"


@dataclass
class Verdict:
    pr: int
    verdict: str = "ok"
    reasons: list[Reason] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str, message: str) -> None:
        self.reasons.append(Reason(kind, message))
        if kind == "block":
            self.verdict = "block"
        elif self.verdict == "ok":
            self.verdict = "review"

    def as_dict(self) -> dict:
        return {
            "pr": self.pr,
            "verdict": self.verdict,
            "reasons": [reason.render() for reason in self.reasons],
            "stats": self.stats,
        }


def _path_matches(pattern: str, path: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, f"*/{pattern}"):
        return True
    return path == pattern or path.endswith(f"/{pattern}")


def _paths_match(patterns: list[str], path: str) -> bool:
    return any(_path_matches(pattern, path) for pattern in patterns)


def _rule_applies(rule: dict, path: str) -> bool:
    patterns = rule.get("paths")
    return True if not patterns else _paths_match(patterns, path)


def _line_totals(root: Path, base: str, head: str) -> tuple[int, int]:
    raw = git(root, "diff", "--numstat", "--no-renames", f"{base}...{head}").decode(
        "utf-8", "replace"
    )
    added = removed = 0
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        if parts[0].isdigit():
            added += int(parts[0])
        if parts[1].isdigit():
            removed += int(parts[1])
    return added, removed


def evaluate(
    root: Path,
    *,
    pr: int,
    base: str,
    head: str,
    policy: dict,
    author: str | None,
    title: str | None,
) -> Verdict:
    verdict = Verdict(pr=pr)

    allow = policy.get("allow", {})
    if str(pr) in allow:
        verdict.reasons.append(Reason("allow", f"PR #{pr} is allowed: {allow[str(pr)]}"))
        return verdict

    skip = policy.get("skip", {})
    if str(pr) in skip:
        verdict.add("review", f"PR #{pr} is on the skip list: {skip[str(pr)]}")
        return verdict

    skip_authors = {login.lower() for login in policy.get("skip_authors", [])}
    if author and author.lower() in skip_authors:
        verdict.add("review", f"author {author} is on the skip list")

    if title:
        for pattern in policy.get("review_title_patterns", []):
            if re.search(pattern, title):
                verdict.add("review", f"title matches {pattern!r}: {title!r}")

    changes = changed_paths(root, "--diff-filter=AMRD", f"{base}...{head}")
    paths = [path for _status, _mode, path in changes]
    added, removed = _line_totals(root, base, head)
    verdict.stats = {
        "files": len(paths),
        "added_lines": added,
        "removed_lines": removed,
    }

    for path in paths:
        for pattern, reason in policy.get("blocked_paths", {}).items():
            if _path_matches(pattern, path):
                verdict.add("block", f"{path}: {reason}")
                break
        else:
            for pattern, reason in policy.get("review_paths", {}).items():
                if _path_matches(pattern, path):
                    verdict.add("review", f"{path}: {reason}")
                    break

    for path, line_no, text in added_lines(root, base, head):
        for rule in policy.get("blocked_lines", []):
            if _rule_applies(rule, path) and re.search(rule["pattern"], text):
                snippet = " ".join(text.strip().split())[:120]
                verdict.add("block", f"{path}:{line_no}: {rule['reason']} ({snippet})")

    thresholds = policy.get("review_thresholds", {})
    max_files = thresholds.get("max_files")
    max_lines = thresholds.get("max_changed_lines")
    if max_files and len(paths) > max_files:
        verdict.add("review", f"{len(paths)} files changed, limit is {max_files}")
    if max_lines and added + removed > max_lines:
        verdict.add("review", f"{added + removed} changed lines, limit is {max_lines}")

    return verdict


def load_policy(root: Path, ref: str | None) -> dict:
    if ref is None:
        target = root / POLICY_PATH
        if not target.is_file():
            raise ValueError(f"missing {POLICY_PATH}")
        text = target.read_text(encoding="utf-8")
    else:
        text = _show(root, ref, POLICY_PATH)
    policy = json.loads(text)
    if policy.get("version") != 1:
        raise ValueError(f"{POLICY_PATH}: unsupported version {policy.get('version')!r}")
    return policy


def _show(root: Path, ref: str, path: str) -> str:
    try:
        return git(root, "show", f"{ref}:{path}").decode("utf-8")
    except GitError as exc:
        raise ValueError(f"{path} is missing at {ref}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument("--pr", type=int, required=True, help="pull request number")
    parser.add_argument("--base", required=True, help="trusted base revision")
    parser.add_argument("--head", required=True, help="pull request head revision")
    parser.add_argument("--author", help="pull request author login")
    parser.add_argument("--title", help="pull request title")
    parser.add_argument(
        "--policy-ref",
        help=f"read {POLICY_PATH} from this revision instead of the working tree",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    root = repo_root(Path(args.root).resolve())
    try:
        policy = load_policy(root, args.policy_ref)
    except (ValueError, OSError) as exc:
        print(f"scan_pr: {exc}", file=sys.stderr)
        return 2

    verdict = evaluate(
        root,
        pr=args.pr,
        base=args.base,
        head=args.head,
        policy=policy,
        author=args.author,
        title=args.title,
    )

    if args.json:
        print(json.dumps(verdict.as_dict()))
    else:
        print(f"PR #{args.pr}: {verdict.verdict} {verdict.stats}")
        for reason in verdict.reasons:
            print(f"  {reason.render()}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            level = "error" if verdict.verdict == "block" else "warning"
            for reason in verdict.reasons:
                print(f"::{level}::PR #{args.pr}: {reason.message}")

    return {"ok": EXIT_OK, "review": EXIT_REVIEW, "block": EXIT_BLOCK}[verdict.verdict]


if __name__ == "__main__":
    sys.exit(main())

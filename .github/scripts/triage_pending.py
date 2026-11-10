#!/usr/bin/env python3
"""Split pending upstream pull requests into admitted and held.

Runs both gates (``scan_blobs`` for binaries and encoded payloads,
``scan_pr`` for the admission policy) against every pending pull request and
writes:

* ``--admitted``: ``<number> <sha>`` lines the merge step may merge,
* ``--summary``: a Markdown table for the workflow run summary.

Anything that should not land is held back, and the repository keeps merging
the rest; a gate crash aborts the run instead, so a broken gate can never be
mistaken for a clean one.

Exit codes: 0 completed, 2 internal error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _gitutil import GitError, repo_root
from scan_blobs import scan
from scan_pr import evaluate, load_policy


def _read_pending(path: Path) -> list[tuple[int, str]]:
    pending = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        pending.append((int(parts[0]), parts[1]))
    return pending


def _table_row(cells: list[str]) -> str:
    escaped = [cell.replace("|", "\\|") for cell in cells]
    return "| " + " | ".join(escaped) + " |"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".")
    parser.add_argument("--base", required=True, help="trusted base revision")
    parser.add_argument("--pending", required=True, type=Path)
    parser.add_argument("--meta", type=Path, help="JSON map of pr -> {author, title}")
    parser.add_argument("--admitted", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    args = parser.parse_args(argv)

    root = repo_root(Path(args.root).resolve())
    try:
        policy = load_policy(root, args.base)
        pending = _read_pending(args.pending)
        meta = {}
        if args.meta and args.meta.is_file():
            meta = json.loads(args.meta.read_text(encoding="utf-8"))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"triage_pending: {exc}", file=sys.stderr)
        return 2

    admitted: list[str] = []
    rows = ["| PR | Verdict | Findings |", "| --- | --- | --- |"]
    for number, sha in pending:
        info = meta.get(str(number), {})
        author = info.get("author")
        title = info.get("title")
        try:
            blobs = scan(
                root,
                rev=sha,
                base=args.base,
                staged=False,
                allowlist_ref=args.base,
                protect_security_files=True,
            )
            verdict = evaluate(
                root,
                pr=number,
                base=args.base,
                head=sha,
                policy=policy,
                author=author,
                title=title,
            )
        except GitError as exc:
            print(f"triage_pending: PR #{number}: {exc}", file=sys.stderr)
            return 2

        findings = [finding.render() for finding in blobs] + [
            reason.message for reason in verdict.reasons if reason.kind != "allow"
        ]
        if blobs or verdict.verdict == "block":
            result = "block"
        elif verdict.verdict == "review":
            result = "review"
        else:
            result = "ok"
            admitted.append(f"{number} {sha}")

        print(f"PR #{number}: {result}")
        for finding in findings:
            print(f"  {finding}")
        rows.append(_table_row([f"#{number}", result, "<br>".join(findings) or "—"]))

    args.admitted.write_text(
        "".join(f"{line}\n" for line in admitted), encoding="utf-8"
    )
    held = len(pending) - len(admitted)
    header = [
        "### Upstream merge",
        "",
        f"Admitted {len(admitted)} of {len(pending)} pending pull request(s); "
        f"{held} held for review.",
        "",
    ]
    args.summary.write_text("\n".join(header + rows) + "\n", encoding="utf-8")
    print(f"admitted {len(admitted)}, held {held}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

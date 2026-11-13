#!/usr/bin/env python3
"""Increment the project version by one patch release.

Every upstream pull request the auto-merge lands gets its own version, so each
merged PR ships as ``x.y.N`` and a published package maps to exactly one merge:
1.1.178, 1.1.179, 1.1.180 and so on.

The counter lives in ``.github/release-version.txt``, not in the version string
itself, because upstream pull requests edit ``pyproject.toml``: reading the
number back out of a file upstream can clobber would let it go backwards. The
file under ``.github/`` is on the admission policy's blocked list, so upstream
cannot touch it. Each bump rewrites both:

* the counter, incremented by one patch level,
* the ``version`` line of ``[project]`` in ``pyproject.toml``, so a merge that
  brought an older version string is repaired rather than trusted.

Only that one line is rewritten (the pattern is anchored to a whole line
starting with ``version = ``), so dependency constraints such as
``tree-sitter-language-pack>=0.3,<1.0`` are never touched.

Exit codes: 0 bumped, 2 missing or malformed version.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

DEFAULT_COUNTER = ".github/release-version.txt"
VERSION_LINE = re.compile(r'(?m)^version\s*=\s*"[^"]*"$')


def parse(version: str) -> tuple[int, int, int]:
    parts = version.strip().split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"expected a numeric x.y.z version, got {version!r}")
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch


def increment(version: str) -> str:
    major, minor, patch = parse(version)
    return f"{major}.{minor}.{patch + 1}"


def rewrite_pyproject(text: str, version: str) -> str:
    new_text, replaced = VERSION_LINE.subn(f'version = "{version}"', text, count=1)
    if replaced != 1:
        raise ValueError(f"expected one version line in pyproject.toml, found {replaced}")
    try:
        read_back = tomllib.loads(new_text)["project"]["version"]
    except (KeyError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"pyproject.toml has no usable [project] version: {exc}") from exc
    if read_back != version:
        raise ValueError(f"rewrote the version line but read back {read_back!r}")
    return new_text


def bump(counter_file: Path, pyproject: Path) -> str:
    version = increment(counter_file.read_text(encoding="utf-8"))
    text = rewrite_pyproject(pyproject.read_text(encoding="utf-8"), version)
    counter_file.write_text(f"{version}\n", encoding="utf-8")
    pyproject.write_text(text, encoding="utf-8")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--counter", default=DEFAULT_COUNTER, help="release counter file")
    parser.add_argument("--pyproject", default="pyproject.toml", help="packaging metadata")
    parser.add_argument("--show", action="store_true", help="print the version without bumping")
    args = parser.parse_args(argv)

    counter_file = Path(args.counter)
    try:
        if args.show:
            print(counter_file.read_text(encoding="utf-8").strip())
            return 0
        print(bump(counter_file, Path(args.pyproject)))
    except (OSError, ValueError) as exc:
        print(f"bump_version: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

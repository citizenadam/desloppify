"""Git plumbing shared by the repository's CI gate scripts.

Standard library only: the workflow steps that run these scripts install
nothing first.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(root: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout


def repo_root(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").decode().strip())


def tree_entries(root: Path, rev: str) -> list[tuple[str, str, str]]:
    """(path, mode, object spec) for every blob in ``rev``."""
    entries = []
    for record in git(root, "ls-tree", "-r", "-z", rev).split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        mode, kind, _sha = meta.split()
        if kind != b"blob":
            continue
        path = raw_path.decode("utf-8", "surrogateescape")
        entries.append((path, mode.decode(), f"{rev}:{path}"))
    return entries


def changed_paths(root: Path, *diff_args: str) -> list[tuple[str, str, str]]:
    """(status, new mode, new path) for each change reported by `git diff --raw`."""
    raw = git(root, "diff", "--raw", "-z", *diff_args)
    fields = raw.split(b"\0")
    changes: list[tuple[str, str, str]] = []
    pos = 0
    while pos < len(fields) and fields[pos]:
        _old_mode, new_mode, _old_sha, _new_sha, status = (
            fields[pos].decode()[1:].split()
        )
        if status[0] in "RC":
            new_path = fields[pos + 2].decode("utf-8", "surrogateescape")
            pos += 3
        else:
            new_path = fields[pos + 1].decode("utf-8", "surrogateescape")
            pos += 2
        changes.append((status, new_mode, new_path))
    return changes


def read_blobs(root: Path, specs: list[str]) -> list[bytes | None]:
    """Blob contents for ``specs`` in order, from a single cat-file process."""
    if not specs:
        return []
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        input=("\n".join(specs) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git cat-file failed: {detail}")
    out = proc.stdout
    contents: list[bytes | None] = []
    pos = 0
    for _spec in specs:
        newline = out.find(b"\n", pos)
        if newline < 0:
            raise GitError("git cat-file returned a truncated batch")
        header = out[pos:newline]
        pos = newline + 1
        if header.endswith((b" missing", b" ambiguous")):
            contents.append(None)
            continue
        _sha, _kind, size = header.rsplit(b" ", 2)
        length = int(size)
        contents.append(out[pos : pos + length])
        pos += length + 1
    return contents


def working_tree_entries(root: Path) -> list[tuple[str, str, str]]:
    """(path, mode, path) for every file in the index."""
    entries = []
    for record in git(root, "ls-files", "-s", "-z").split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        mode, _sha, _stage = meta.split()
        path = raw_path.decode("utf-8", "surrogateescape")
        entries.append((path, mode.decode(), path))
    return entries


def read_working_tree(root: Path, paths: list[str]) -> list[bytes | None]:
    contents: list[bytes | None] = []
    for path in paths:
        try:
            contents.append((root / path).read_bytes())
        except OSError:
            contents.append(None)
    return contents


def added_lines(root: Path, base: str, head: str) -> list[tuple[str, int, str]]:
    """(path, new-file line, text) for every added line in ``base...head``."""
    diff = git(root, "diff", "--text", "--no-color", "-U0", f"{base}...{head}").decode(
        "utf-8", "replace"
    )
    added: list[tuple[str, int, str]] = []
    path = ""
    line_no = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = "" if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@"):
            # @@ -a,b +c,d @@ -> next added line starts at c
            try:
                line_no = int(line.split("+", 1)[1].split(",", 1)[0].split(" ", 1)[0])
            except (IndexError, ValueError):
                line_no = 0
        elif line.startswith("+") and not line.startswith("+++"):
            added.append((path, line_no, line[1:]))
            line_no += 1
    return added

#!/usr/bin/env python3
"""Reject binaries, archives and encoded payloads before they reach `main`.

The upstream auto-merge commits every upstream pull request without review, so
a single committed binary would be published to PyPI on the following push.
This gate runs on the pull-request head before anything is merged, and again
on the merged tree before the push.

Standard library only: the CI job that runs it installs nothing first, so a
third-party scanner could not be installed there either.

Policy
------
* Executables, shared libraries, archives, bytecode and serialised objects are
  always rejected, whatever the file is named and whether or not it is
  registered (a pickled object executes on load).
* Other non-text files are accepted only when their sha256 is registered in
  ``.github/blob-allowlist.txt``. Pull requests coming from upstream may not
  edit that allowlist or this gate (``--protect-security-files``), so they
  cannot approve their own binary.
* Text files are accepted up to ``MAX_TEXT_BYTES``. A quoted base64 or hex
  literal longer than ``MAX_ENCODED_RUN`` characters is treated as a smuggled
  payload.
* Symbolic links, NUL-containing files and invalid UTF-8 are rejected: the
  repository holds none of them (measured over 1801 tracked files), so their
  appearance is a signal rather than a false positive.

Thresholds are measured against this tree, not guessed: the largest tracked
text file is 98 KB, the largest registered asset is 1.5 MB, and the longest
base64-alphabet run across 1796 tracked text files is 85 characters.

Exit codes: 0 clean, 1 findings, 2 configuration error.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOWLIST_PATH = ".github/blob-allowlist.txt"
SAMPLE_BYTES = 8192

# 1 MiB: ten times the largest tracked text file.
MAX_TEXT_BYTES = 1 << 20
# 2 MiB: above the largest registered asset (1.5 MB).
MAX_ASSET_BYTES = 1 << 21
# 512: six times the longest base64-alphabet run in the tree (85 chars).
MAX_ENCODED_RUN = 512

# Extensions that are never plain text. Values are the reason shown to the
# contributor. These are rejected even when an allowlist entry matches.
BLOCKED_EXTENSIONS = {
    ".a": "static library",
    ".apk": "Android package",
    ".bin": "binary blob",
    ".bz2": "compressed archive",
    ".class": "Java bytecode",
    ".com": "DOS executable",
    ".deb": "Debian package",
    ".dex": "Dalvik executable",
    ".dll": "Windows library",
    ".dmg": "disk image",
    ".drv": "kernel driver",
    ".dylib": "shared library",
    ".ear": "Java archive",
    ".egg": "Python package archive",
    ".elf": "ELF executable",
    ".exe": "executable",
    ".gz": "compressed archive",
    ".iso": "disk image",
    ".jar": "Java archive",
    ".joblib": "serialised object (executes on load)",
    ".ko": "kernel module",
    ".lib": "import library",
    ".msi": "Windows installer",
    ".node": "native addon",
    ".npy": "array blob",
    ".o": "object file",
    ".obj": "object file",
    ".pkl": "pickle (executes on load)",
    ".pickle": "pickle (executes on load)",
    ".pyc": "compiled bytecode",
    ".pyd": "compiled extension",
    ".pyo": "compiled bytecode",
    ".rar": "compressed archive",
    ".rpm": "Red Hat package",
    ".scr": "screen saver executable",
    ".so": "shared library",
    ".sys": "kernel driver",
    ".tar": "archive",
    ".tgz": "compressed archive",
    ".war": "Java archive",
    ".wasm": "WebAssembly module",
    ".whl": "Python wheel",
    ".xz": "compressed archive",
    ".7z": "compressed archive",
    ".zip": "compressed archive",
}

# Byte prefixes that identify a container format regardless of the file name.
SIGNATURES = (
    (b"\x7fELF", "ELF executable"),
    (b"MZ", "PE executable"),
    (b"\xfe\xed\xfa\xce", "Mach-O executable"),
    (b"\xfe\xed\xfa\xcf", "Mach-O executable"),
    (b"\xce\xfa\xed\xfe", "Mach-O executable"),
    (b"\xcf\xfa\xed\xfe", "Mach-O executable"),
    (b"\xca\xfe\xba\xbe", "Mach-O universal or Java class"),
    (b"dex\n", "Dalvik executable"),
    (b"\x00asm", "WebAssembly module"),
    (b"PK\x03\x04", "ZIP archive"),
    (b"PK\x05\x06", "ZIP archive"),
    (b"\x1f\x8b", "gzip archive"),
    (b"BZh", "bzip2 archive"),
    (b"\xfd7zXZ\x00", "xz archive"),
    (b"7z\xbc\xaf\x27\x1c", "7z archive"),
    (b"Rar!\x1a\x07", "RAR archive"),
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF87a", "GIF image"),
    (b"GIF89a", "GIF image"),
    (b"RIFF", "RIFF container"),
    (b"%PDF-", "PDF document"),
    (b"SQLite format 3\x00", "SQLite database"),
    (b"\xd0\xcf\x11\xe0", "OLE compound file"),
    (b"\x1aE\xdf\xa3", "Matroska container"),
    (b"OggS", "Ogg container"),
    (b"ID3", "MP3 audio"),
)

# Files whose change must be reviewed by a human: they decide what the gate
# allows and when it runs. Upstream pull requests may not touch them.
PROTECTED_PATHS = (
    ALLOWLIST_PATH,
    ".github/scripts/scan_blobs.py",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".github/workflows/python-publish.yml",
    ".github/workflows/upstream-merge.yml",
    ".githooks/pre-commit",
)

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_BASE64_RUN_RE = re.compile(
    r"""(?P<quote>['"])(?P<blob>[A-Za-z0-9+/]{"""
    + str(MAX_ENCODED_RUN)
    + r""",}={0,2})(?P=quote)"""
)


@dataclass(frozen=True)
class Finding:
    path: str
    reason: str
    line: int | None = None

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location}: {self.reason}"


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str
    spec: str


class GitError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def _repo_root(path: Path) -> Path:
    return Path(_git(path, "rev-parse", "--show-toplevel").decode().strip())


def _tree_entries(root: Path, rev: str) -> list[Entry]:
    """Entries of ``rev`` from one `git ls-tree` call."""
    raw = _git(root, "ls-tree", "-r", "-z", rev)
    entries: list[Entry] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, kind, _sha = meta.split()
        if kind != b"blob":
            continue
        decoded = path.decode("utf-8", "surrogateescape")
        entries.append(Entry(path=decoded, mode=mode.decode(), spec=f"{rev}:{decoded}"))
    return entries


def _changed_paths(root: Path, *args: str) -> list[tuple[str, str, str]]:
    """(status, new mode, new path) for each change reported by `git diff --raw`."""
    raw = _git(root, "diff", "--raw", "-z", *args)
    fields = raw.split(b"\0")
    changes: list[tuple[str, str, str]] = []
    pos = 0
    while pos < len(fields) and fields[pos]:
        old_mode, new_mode, _old_sha, _new_sha, status = (
            fields[pos].decode()[1:].split()
        )
        del old_mode
        if status[0] in "RC":
            new_path = fields[pos + 2].decode("utf-8", "surrogateescape")
            pos += 3
        else:
            new_path = fields[pos + 1].decode("utf-8", "surrogateescape")
            pos += 2
        changes.append((status, new_mode, new_path))
    return changes


def _entries_for_changes(
    changes: list[tuple[str, str, str]], spec_prefix: str
) -> list[Entry]:
    return [
        Entry(path=path, mode=mode, spec=f"{spec_prefix}{path}")
        for status, mode, path in changes
        if status[0] in "AMR"
    ]


def _read_blobs(root: Path, specs: list[str]) -> list[bytes | None]:
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
        raise GitError(
            f"git cat-file failed: {proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    out = proc.stdout
    contents: list[bytes | None] = []
    pos = 0
    for _spec in specs:
        newline = out.find(b"\n", pos)
        if newline < 0:
            raise GitError("git cat-file returned a truncated batch")
        header = out[pos:newline]
        pos = newline + 1
        if header.endswith(b" missing") or header.endswith(b" ambiguous"):
            contents.append(None)
            continue
        _sha, _kind, size = header.rsplit(b" ", 2)
        length = int(size)
        contents.append(out[pos : pos + length])
        pos += length + 1
    return contents


def _working_tree_entries(root: Path) -> list[Entry]:
    raw = _git(root, "ls-files", "-s", "-z")
    entries: list[Entry] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, path_bytes = record.split(b"\t", 1)
        mode, _sha, _stage = meta.split()
        path = path_bytes.decode("utf-8", "surrogateescape")
        entries.append(Entry(path=path, mode=mode.decode(), spec=path))
    return entries


def _read_working_tree(root: Path, entries: list[Entry]) -> list[bytes | None]:
    contents: list[bytes | None] = []
    for entry in entries:
        try:
            contents.append((root / entry.path).read_bytes())
        except OSError:
            contents.append(None)
    return contents


def _diff_entries(
    root: Path, base: str, head: str
) -> tuple[list[Entry], list[tuple[str, str]]]:
    """(entries added by ``base...head``, every (status, path) change)."""
    changes = _changed_paths(root, "--diff-filter=AMRD", f"{base}...{head}")
    entries = _entries_for_changes(changes, f"{head}:")
    return entries, [(status, path) for status, _mode, path in changes]


def _blocked_extension(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    return BLOCKED_EXTENSIONS.get(suffix)


def _signature(data: bytes) -> str | None:
    head = data[:32]
    for prefix, label in SIGNATURES:
        if head.startswith(prefix):
            return label
    return None


def _decodes_as_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _encoded_literal(text: str) -> tuple[int, str] | None:
    """(line, kind) of the first oversized literal, if any."""
    for match in _BASE64_RUN_RE.finditer(text):
        blob = match.group("blob")
        line = text.count("\n", 0, match.start()) + 1
        kind = "hex literal" if _HEX_RE.match(blob) else "base64 literal"
        return line, f"{kind} of {len(blob)} characters"
    return None


def check_file(
    path: str,
    data: bytes,
    mode: str,
    *,
    allowlist: dict[str, str],
) -> list[Finding]:
    """Findings for one path's content, independent of how it was read."""
    if mode == "120000":
        return [Finding(path, "symbolic link (not plain code)")]

    blocked = _blocked_extension(path)
    if blocked is not None:
        return [Finding(path, f"{Path(path).suffix.lower()} is a {blocked}; never accepted")]

    size = len(data)
    is_text = b"\0" not in data[:SAMPLE_BYTES] and _decodes_as_utf8(data)

    if not is_text:
        label = _signature(data) or "binary content"
        digest = hashlib.sha256(data).hexdigest()
        registered = allowlist.get(path)
        if registered is not None and registered == digest:
            if size > MAX_ASSET_BYTES:
                return [
                    Finding(
                        path,
                        f"registered asset is {size} bytes, above the {MAX_ASSET_BYTES} byte cap",
                    )
                ]
            return []
        if registered is not None:
            return [
                Finding(
                    path,
                    f"changed {label} no longer matches the registered sha256 "
                    f"(registered {registered[:12]}..., now {digest[:12]}...)",
                )
            ]
        return [
            Finding(
                path,
                f"{label} is not registered in {ALLOWLIST_PATH} "
                f"(sha256 {digest[:12]}...)",
            )
        ]

    if size > MAX_TEXT_BYTES:
        return [Finding(path, f"text file is {size} bytes, above the {MAX_TEXT_BYTES} byte cap")]

    literal = _encoded_literal(data.decode("utf-8"))
    if literal is not None:
        line, kind = literal
        return [Finding(path, f"oversized {kind}", line=line)]
    return []


def _load_allowlist(text: str) -> dict[str, str]:
    allowlist: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise ValueError(
                f"{ALLOWLIST_PATH}:{number}: expected '<sha256>  <path>', got {raw_line!r}"
            )
        digest, path = parts
        if path in allowlist and allowlist[path] != digest:
            raise ValueError(f"{ALLOWLIST_PATH}:{number}: duplicate entry for {path}")
        allowlist[path] = digest
    return allowlist


def _allowlist_text(root: Path, ref: str | None) -> str:
    if ref is None:
        target = root / ALLOWLIST_PATH
        if not target.is_file():
            raise ValueError(f"missing {ALLOWLIST_PATH}")
        return target.read_text(encoding="utf-8")
    proc = subprocess.run(
        ["git", "show", f"{ref}:{ALLOWLIST_PATH}"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError(f"{ALLOWLIST_PATH} is missing at {ref}")
    return proc.stdout.decode("utf-8")


def _protected_findings(changes: list[tuple[str, str]]) -> list[Finding]:
    findings = []
    for status, path in changes:
        if not any(path == protected or path.startswith(protected.rstrip("/") + "/") for protected in PROTECTED_PATHS):
            continue
        findings.append(
            Finding(
                path,
                f"changes a file the binary/blob gate depends on ({status}); "
                "upstream merges may not alter the gate itself",
            )
        )
    return findings


def scan(
    root: Path,
    *,
    rev: str | None,
    base: str | None,
    staged: bool,
    allowlist_ref: str | None,
    protect_security_files: bool,
) -> list[Finding]:
    try:
        allowlist = _load_allowlist(_allowlist_text(root, allowlist_ref))
    except ValueError as exc:
        raise SystemExit(f"scan_blobs: {exc}") from exc

    changes: list[tuple[str, str]] = []
    if staged:
        staged_changes = _changed_paths(root, "--cached", "--diff-filter=AMR")
        entries = _entries_for_changes(staged_changes, ":")
        contents = _read_blobs(root, [entry.spec for entry in entries])
    elif base is not None:
        head = rev or "HEAD"
        entries, changes = _diff_entries(root, base, head)
        contents = _read_blobs(root, [entry.spec for entry in entries])
    elif rev is not None:
        entries = _tree_entries(root, rev)
        contents = _read_blobs(root, [entry.spec for entry in entries])
    else:
        entries = _working_tree_entries(root)
        contents = _read_working_tree(root, entries)

    findings: list[Finding] = []
    if protect_security_files and changes:
        findings.extend(_protected_findings(changes))
    for entry, data in zip(entries, contents, strict=True):
        if data is None:
            findings.append(Finding(entry.path, "could not be read"))
            continue
        findings.extend(check_file(entry.path, data, entry.mode, allowlist=allowlist))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", default=".", help="repository root (default: current directory)"
    )
    parser.add_argument(
        "--all", action="store_true", help="scan every tracked file (default)"
    )
    parser.add_argument("--ref", help="scan the tree at this revision")
    parser.add_argument(
        "--base",
        help="scan files added, modified or renamed between this revision and --ref",
    )
    parser.add_argument(
        "--staged", action="store_true", help="scan files staged in the index"
    )
    parser.add_argument(
        "--allowlist-ref",
        help=f"read {ALLOWLIST_PATH} from this revision instead of the working tree",
    )
    parser.add_argument(
        "--protect-security-files",
        action="store_true",
        help="reject changes to the gate, its allowlist or its workflows",
    )
    args = parser.parse_args(argv)

    if args.staged and (args.base or args.ref):
        parser.error("--staged cannot be combined with --ref or --base")
    if args.base and not args.ref:
        parser.error("--base requires --ref")

    root = _repo_root(Path(args.root).resolve())
    findings = scan(
        root,
        rev=args.ref,
        base=args.base,
        staged=args.staged,
        allowlist_ref=args.allowlist_ref,
        protect_security_files=args.protect_security_files,
    )

    for finding in findings:
        print(finding.render())
        if os.environ.get("GITHUB_ACTIONS") == "true":
            location = f" file={finding.path}"
            if finding.line:
                location += f",line={finding.line}"
            print(f"::error{location}::{finding.reason}")
    if findings:
        print(f"scan_blobs: {len(findings)} blocking finding(s)")
        return 1
    print("scan_blobs: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())

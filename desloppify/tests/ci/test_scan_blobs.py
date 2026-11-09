from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "scan_blobs.py"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
ELF = b"\x7fELF" + b"\x00" * 28


@pytest.fixture(scope="module")
def scanner():
    spec = importlib.util.spec_from_file_location("ci_scan_blobs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolves annotations through sys.modules during exec.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _findings(scanner, path: str, data: bytes, mode: str = "100644", allowlist=None):
    return scanner.check_file(path, data, mode, allowlist=allowlist or {})


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- content rules -------------------------------------------------------


def test_tracked_tree_is_clean(scanner) -> None:
    assert scanner.main(["--root", str(REPO_ROOT)]) == 0


def test_executable_signature_is_rejected_whatever_the_name(scanner) -> None:
    findings = _findings(scanner, "desloppify/notes.txt", ELF)
    assert len(findings) == 1
    assert "ELF executable" in findings[0].reason


def test_blocked_extension_cannot_be_allowlisted(scanner) -> None:
    payload = b"c\x00\x00pickle"
    findings = _findings(scanner, "desloppify/data.pkl", payload, allowlist={"desloppify/data.pkl": _hash(payload)})
    assert len(findings) == 1
    assert "never accepted" in findings[0].reason


def test_registered_asset_is_accepted(scanner) -> None:
    allowlist = {"assets/mascot.png": _hash(PNG)}
    assert _findings(scanner, "assets/mascot.png", PNG, allowlist=allowlist) == []


def test_unregistered_asset_is_rejected_with_its_hash(scanner) -> None:
    findings = _findings(scanner, "assets/new.png", PNG)
    assert len(findings) == 1
    assert "PNG image" in findings[0].reason
    assert _hash(PNG)[:12] in findings[0].reason


def test_changed_asset_breaks_its_registration(scanner) -> None:
    allowlist = {"assets/mascot.png": _hash(PNG + b"tampered")}
    findings = _findings(scanner, "assets/mascot.png", PNG, allowlist=allowlist)
    assert len(findings) == 1
    assert "no longer matches" in findings[0].reason


def test_asset_above_the_cap_is_rejected(scanner) -> None:
    oversized = PNG + b"\x00" * (scanner.MAX_ASSET_BYTES + 1)
    allowlist = {"assets/huge.png": _hash(oversized)}
    findings = _findings(scanner, "assets/huge.png", oversized, allowlist=allowlist)
    assert len(findings) == 1
    assert "above the" in findings[0].reason


def test_oversized_text_is_rejected(scanner) -> None:
    findings = _findings(scanner, "desloppify/blob.py", b"a" * (scanner.MAX_TEXT_BYTES + 1))
    assert len(findings) == 1
    assert "byte cap" in findings[0].reason


def test_base64_literal_is_rejected(scanner) -> None:
    blob = "QUJD" * 200
    data = f'HEADER = 1\nPAYLOAD = "{blob}"\n'.encode()
    findings = _findings(scanner, "desloppify/loader.py", data)
    assert len(findings) == 1
    assert findings[0].line == 2
    assert "base64 literal" in findings[0].reason


def test_hex_literal_is_rejected(scanner) -> None:
    blob = "deadbeef" * 80
    data = f'PAYLOAD = "{blob}"\n'.encode()
    findings = _findings(scanner, "desloppify/loader.py", data)
    assert len(findings) == 1
    assert "hex literal" in findings[0].reason


def test_long_quoted_literal_is_rejected_even_without_base64_punctuation(scanner) -> None:
    data = f'SLUG = "{"ab" * 300}"\n'.encode()
    findings = _findings(scanner, "desloppify/names.py", data)
    assert len(findings) == 1
    assert "literal of 600 characters" in findings[0].reason


def test_ordinary_strings_are_not_treated_as_payload(scanner) -> None:
    url = "https://example.com/" + "segment/" * 60
    data = f'URL = "{url}"\n'.encode()
    assert _findings(scanner, "desloppify/names.py", data) == []


def test_symbolic_link_is_rejected(scanner) -> None:
    findings = _findings(scanner, "desloppify/link.py", b"../../etc/passwd", mode="120000")
    assert len(findings) == 1
    assert "symbolic link" in findings[0].reason


def test_invalid_utf8_is_rejected(scanner) -> None:
    findings = _findings(scanner, "desloppify/data.txt", b"caf\xe9")
    assert len(findings) == 1


# --- repository / diff rules ---------------------------------------------


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, env=_git_env()
    ).stdout.decode()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / ".github" / "scripts").mkdir(parents=True)
    (tmp_path / ".github" / "blob-allowlist.txt").write_text("", encoding="utf-8")
    (tmp_path / ".github" / "scripts" / "scan_blobs.py").write_text("# gate\n", encoding="utf-8")
    (tmp_path / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit(tmp_path, "base")
    return tmp_path


def test_protected_gate_file_cannot_change(scanner, repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / ".github" / "scripts" / "scan_blobs.py").write_text("# weakened\n", encoding="utf-8")
    head = _commit(repo, "weaken the gate")

    findings = scanner.scan(
        repo,
        rev=head,
        base=base,
        staged=False,
        allowlist_ref=base,
        protect_security_files=True,
    )

    assert [f.path for f in findings] == [".github/scripts/scan_blobs.py"]
    assert "depends on" in findings[0].reason


def test_plain_text_change_is_accepted(scanner, repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    head = _commit(repo, "bump value")

    findings = scanner.scan(
        repo,
        rev=head,
        base=base,
        staged=False,
        allowlist_ref=base,
        protect_security_files=True,
    )

    assert findings == []


def test_pull_request_cannot_approve_its_own_binary(scanner, repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    binary = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    (repo / "assets").mkdir()
    (repo / "assets" / "payload.png").write_bytes(binary)
    (repo / ".github" / "blob-allowlist.txt").write_text(
        f"{_hash(binary)}  assets/payload.png\n", encoding="utf-8"
    )
    head = _commit(repo, "register and add a binary")

    # The gate reads the allowlist from the trusted base ref, so the entry the
    # pull request added does not count.
    findings = scanner.scan(
        repo,
        rev=head,
        base=base,
        staged=False,
        allowlist_ref=base,
        protect_security_files=True,
    )

    paths = sorted({finding.path for finding in findings})
    assert paths == [".github/blob-allowlist.txt", "assets/payload.png"]
    assert any("not registered" in finding.reason for finding in findings)


def test_allowlist_parser_rejects_malformed_lines(scanner) -> None:
    with pytest.raises(ValueError, match="expected"):
        scanner._load_allowlist("not-a-hash  assets/mascot.png\n")


def test_staged_scan_sees_index_content(scanner, repo: Path) -> None:
    (repo / "module.py").write_text("PAYLOAD = 1\n", encoding="utf-8")
    _git(repo, "add", "module.py")
    (repo / "payload.bin").write_bytes(b"octet")
    _git(repo, "add", "payload.bin")

    findings = scanner.scan(
        repo,
        rev=None,
        base=None,
        staged=True,
        allowlist_ref=None,
        protect_security_files=False,
    )

    assert [f.path for f in findings] == ["payload.bin"]

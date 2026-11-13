from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / ".github" / "scripts"

sys.path.insert(0, str(SCRIPTS))

PROJECT = """[build-system]
requires = ["setuptools>=68.0"]

[project]
name = "desloppify"
version = "1.1.178"
requires-python = ">=3.11"
dependencies = ["tree-sitter-language-pack>=0.3,<1.0"]
"""


@pytest.fixture(scope="module")
def bumper():
    spec = importlib.util.spec_from_file_location("bump_version", SCRIPTS / "bump_version.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def files(tmp_path: Path) -> tuple[Path, Path]:
    counter = tmp_path / "release-version.txt"
    counter.write_text("1.1.178\n", encoding="utf-8")
    project = tmp_path / "pyproject.toml"
    project.write_text(PROJECT, encoding="utf-8")
    return counter, project


def test_patch_increments_after_178(bumper, files) -> None:
    counter, project = files
    assert bumper.bump(counter, project) == "1.1.179"
    assert 'version = "1.1.179"' in project.read_text(encoding="utf-8")
    assert counter.read_text(encoding="utf-8").strip() == "1.1.179"


def test_dependency_constraints_are_untouched(bumper, files) -> None:
    counter, project = files
    bumper.bump(counter, project)
    text = project.read_text(encoding="utf-8")
    assert "tree-sitter-language-pack>=0.3,<1.0" in text
    assert text.count("1.0") == 1  # only the dependency constraint


def test_rest_of_the_file_is_preserved(bumper, files) -> None:
    counter, project = files
    bumper.bump(counter, project)
    stripped = project.read_text(encoding="utf-8").replace(
        'version = "1.1.179"', 'version = "1.1.178"'
    )
    assert stripped == PROJECT


def test_a_merge_that_reintroduces_an_old_version_is_repaired(bumper, files) -> None:
    counter, project = files
    project.write_text(PROJECT.replace('"1.1.178"', '"1.0"'), encoding="utf-8")

    assert bumper.bump(counter, project) == "1.1.179"
    assert 'version = "1.1.179"' in project.read_text(encoding="utf-8")


def test_ten_increments_walk_the_patch_number(bumper, files) -> None:
    counter, project = files
    versions = [bumper.bump(counter, project) for _ in range(10)]
    assert versions[0] == "1.1.179"
    assert versions[-1] == "1.1.188"


def test_malformed_versions_are_rejected(bumper) -> None:
    with pytest.raises(ValueError, match="numeric x.y.z"):
        bumper.increment("1.1")
    with pytest.raises(ValueError, match="numeric x.y.z"):
        bumper.increment("1.1.178rc1")


def test_missing_version_line_is_rejected(bumper, files) -> None:
    counter, project = files
    project.write_text('[project]\nname = "desloppify"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="expected one version line"):
        bumper.bump(counter, project)
    assert counter.read_text(encoding="utf-8").strip() == "1.1.178", "counter must not advance"


def test_cli_bumps_in_place(bumper, files, capsys) -> None:
    counter, project = files
    args = ["--counter", str(counter), "--pyproject", str(project)]

    assert bumper.main(args) == 0
    assert capsys.readouterr().out.strip() == "1.1.179"
    assert bumper.main([*args, "--show"]) == 0
    assert capsys.readouterr().out.strip() == "1.1.179"


def test_cli_reports_a_broken_counter(bumper, tmp_path: Path, capsys) -> None:
    counter = tmp_path / "release-version.txt"
    counter.write_text("not-a-version\n", encoding="utf-8")
    project = tmp_path / "pyproject.toml"
    project.write_text(PROJECT, encoding="utf-8")

    assert bumper.main(["--counter", str(counter), "--pyproject", str(project)]) == 2
    assert "numeric x.y.z" in capsys.readouterr().err

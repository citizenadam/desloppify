"""Import graphs retain relative file keys without dropping internal edges."""

from pathlib import Path

import pytest

from desloppify.languages._framework.treesitter import JS_SPEC
from desloppify.languages._framework.treesitter.imports.graph import ts_build_dep_graph


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("nested_scan", [False, True])
def test_javascript_graph_connects_relative_and_absolute_files(
    tmp_path: Path, monkeypatch, relative: bool, nested_scan: bool
) -> None:
    pytest.importorskip("tree_sitter_language_pack")
    source = tmp_path / "web" / "main.js"
    dependency = tmp_path / "web" / "helpers.js"
    source.parent.mkdir()
    source.write_text('import {value} from "./helpers.js";\nvalue();\n')
    dependency.write_text("export function value() { return 1; }\n")
    monkeypatch.chdir(tmp_path)
    files = [source, dependency]
    keys = [str(f.relative_to(tmp_path) if relative else f) for f in files]

    graph = ts_build_dep_graph(source.parent if nested_scan else tmp_path, JS_SPEC, keys)

    assert set(graph) == set(keys)
    assert graph[keys[0]]["imports"] == {keys[1]}
    assert graph[keys[1]]["importers"] == {keys[0]}
    assert graph[keys[0]]["import_count"] == 1
    assert graph[keys[1]]["importer_count"] == 1

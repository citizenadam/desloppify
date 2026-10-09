"""PHP import graphs preserve the file keys supplied by discovery."""

from __future__ import annotations

import json

import pytest

from desloppify.languages._framework.treesitter import is_available
from desloppify.languages._framework.treesitter.imports.graph import ts_build_dep_graph
from desloppify.languages._framework.treesitter.specs.compiled import PHP_SPEC

pytestmark = pytest.mark.skipif(
    not is_available(), reason="tree-sitter-language-pack not installed"
)


@pytest.mark.parametrize("absolute_keys", [False, True])
@pytest.mark.parametrize("scan_directory", [".", "app"])
def test_php_graph_preserves_discovered_file_keys(
    monkeypatch, tmp_path, absolute_keys, scan_directory
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "composer.json").write_text(
        json.dumps({"autoload": {"psr-4": {"App\\": "app/"}}})
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app/Foo.php").write_text("<?php namespace App; class Foo {}")
    (tmp_path / "app/Outside.php").write_text("<?php namespace App; class Outside {}")
    (tmp_path / "app/Bar.php").write_text(
        "<?php namespace App; use App\\{Foo, Outside}; use DateTime; class Bar {}"
    )

    foo_key, bar_key = (
        [str(tmp_path / name) for name in ("app/Foo.php", "app/Bar.php")]
        if absolute_keys
        else ["app/Foo.php", "app/Bar.php"]
    )
    graph = ts_build_dep_graph(
        tmp_path / scan_directory, PHP_SPEC, [foo_key, bar_key]
    )

    assert graph == {
        foo_key: {
            "imports": set(),
            "importers": {bar_key},
            "import_count": 0,
            "importer_count": 1,
        },
        bar_key: {
            "imports": {foo_key},
            "importers": set(),
            "import_count": 1,
            "importer_count": 0,
        },
    }


def test_php_graph_resolves_composer_psr4_outside_conventional_roots(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "composer.json").write_text(
        json.dumps({"autoload": {"psr-4": {"Domain\\": "modules/core/"}}})
    )
    (tmp_path / "modules/core").mkdir(parents=True)
    (tmp_path / "modules/core/Foo.php").write_text(
        "<?php namespace Domain; class Foo {}"
    )
    (tmp_path / "main.php").write_text("<?php use Domain\\Foo; new Foo();")

    graph = ts_build_dep_graph(
        tmp_path, PHP_SPEC, ["main.php", "modules/core/Foo.php"]
    )

    assert graph["main.php"]["imports"] == {"modules/core/Foo.php"}
    assert graph["modules/core/Foo.php"]["importers"] == {"main.php"}

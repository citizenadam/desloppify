"""Kotlin language plugin — ktlint, package-aware import graph."""

from desloppify.languages._framework.generic_support.core import generic_lang
from desloppify.languages._framework.treesitter import KOTLIN_SPEC
from desloppify.languages.kotlin.deps import build_kotlin_dep_graph

generic_lang(
    name="kotlin",
    extensions=[".kt", ".kts"],
    tools=[
        {
            "label": "ktlint",
            "cmd": "ktlint --reporter=json",
            "fmt": "json",
            "id": "ktlint_violation",
            "tier": 2,
            "fix_cmd": "ktlint --format",
        },
    ],
    exclude=["build"],
    depth="shallow",
    detect_markers=["build.gradle.kts", "build.gradle"],
    treesitter_spec=KOTLIN_SPEC,
    dep_graph_builder=build_kotlin_dep_graph,
    # Gradle build scripts are run by Gradle, never imported.
    entry_patterns=[".gradle.kts"],
)

__all__ = [
    "generic_lang",
    "KOTLIN_SPEC",
]

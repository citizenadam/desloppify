"""Shared import graph construction utilities for tree-sitter backends."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from desloppify.base.discovery.file_paths import resolve_scan_file

from ..analysis.extractors import _get_parser, _make_query, _run_query, _unwrap_node
from .cache import get_or_parse_tree

if TYPE_CHECKING:
    from desloppify.languages._framework.treesitter import TreeSitterLangSpec


def _source_path(filepath: str) -> Path:
    """Resolve discovery keys against the project root without changing case."""
    return resolve_scan_file(filepath)


def _import_path(filepath: str, scan_path: Path) -> Path:
    """Resolve a resolver result using its scan-root-relative contract."""
    path = Path(filepath)
    if path.is_absolute():
        return path.resolve()
    return (scan_path / path).resolve()


def _path_identity(path: Path) -> str:
    """Return a comparison identity without changing the path used for I/O."""
    return os.path.normcase(str(path))


def ts_build_dep_graph(
    path: Path,
    spec: TreeSitterLangSpec,
    file_list: list[str],
    *,
    framework_extensions: tuple[str, ...] | None = None,
    framework_file_finder: Callable[[Path], list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build a dependency graph by parsing imports with tree-sitter.

    Returns the same shape as Python/TS dep graphs:
    {file: {"imports": set[str], "importers": set[str], "import_count": int, "importer_count": int}}

    When ``framework_extensions`` is provided (e.g. ``(".astro", ".svelte",
    ".vue")``), files under ``path`` with those extensions are also scanned
    via a regex-based import extractor, and their imports are recorded as
    importer edges on matching entries in ``file_list``. The framework files
    themselves are intentionally not added as graph nodes — they don't belong
    to the host language's extension set, so we never want them to surface in
    orphan or coupling reports.

    When ``framework_file_finder`` is provided, it's used to enumerate
    framework files (so callers can supply an exclude-aware finder built via
    ``make_file_finder``). When omitted, a plain ``find_source_files`` scan
    is used — convenient for unit tests but doesn't honor user-configured
    exclusions; production callers should pass a finder.
    """
    if not spec.import_query or not spec.resolve_import:
        return {}

    parser, language = _get_parser(spec.grammar)
    query = _make_query(language, spec.import_query)

    scan_path = str(path.resolve())
    file_set = set(file_list)
    # `resolve_import` returns a path in the same space as the source file it
    # was given, which is not necessarily the space `file_list` uses. Index by
    # absolute path so either space matches.
    abs_index = {os.path.abspath(f): f for f in file_list}
    graph: dict[str, dict[str, Any]] = {}

    # Initialize all files in the graph.
    for f in absolute_file_list:
        graph[f] = {"imports": set(), "importers": set()}

    for filepath in file_list:
        source_path = file_paths_by_key[filepath]
        cached = get_or_parse_tree(str(source_path), parser, spec.grammar)
        if cached is None:
            continue
        _source, tree = cached
        matches = _run_query(query, tree.root_node)

        for _pattern_idx, captures in matches:
            path_node = _unwrap_node(captures.get("path"))
            if not path_node:
                continue

            raw_text = path_node.text
            import_text = (
                raw_text.decode("utf-8", errors="replace")
                if isinstance(raw_text, bytes)
                else str(raw_text)
            )

            # Strip surrounding quotes if present.
            import_text = import_text.strip("\"'`")

            # Prepend group-use prefix when present (PHP ``use A\B\{C, D}``).
            prefix_node = _unwrap_node(captures.get("prefix"))
            if prefix_node is not None:
                prefix_raw = prefix_node.text
                prefix_text = (
                    prefix_raw.decode("utf-8", errors="replace")
                    if isinstance(prefix_raw, bytes)
                    else str(prefix_raw)
                ).strip("\"'`")
                import_text = f"{prefix_text}\\{import_text}"

            resolved = spec.resolve_import(
                import_text, str(source_path), str(scan_path)
            )
            if resolved is None:
                continue

            # Match the resolved path against the file set, whichever path
            # space each happens to use.
            if resolved in file_set:
                target: str | None = resolved
            else:
                target = abs_index.get(os.path.abspath(resolved))
                if target is None:
                    # Fall back to interpreting it as scan_path-relative.
                    candidate = os.path.normpath(os.path.join(scan_path, resolved))
                    target = candidate if candidate in file_set else abs_index.get(candidate)

            # Only track edges within the scanned file set.
            if target is None:
                continue

            graph[filepath]["imports"].add(target)
            if target in graph:
                graph[target]["importers"].add(filepath)

    # Finalize: add counts.
    for data in graph.values():
        data["import_count"] = len(data["imports"])
        data["importer_count"] = len(data["importers"])

    return graph


def _add_framework_importers(
    *,
    graph: dict[str, dict[str, Any]],
    file_set: set[str],
    framework_extensions: tuple[str, ...],
    framework_file_finder: Callable[[Path], list[str]] | None,
    spec: TreeSitterLangSpec,
    scan_path: str,
    path: Path,
) -> None:
    """Add framework files (.astro/.svelte/.vue) as importer edges on graph nodes.

    Framework files carry their imports in fenced or top-level sections that
    the host language's tree-sitter grammar can't parse cleanly. We extract
    import specifiers with a regex and resolve them via the spec's own
    ``resolve_import`` so framework-file imports behave exactly like
    host-language imports — only edges into ``file_set`` are kept, and the
    framework files themselves are not added as graph nodes.
    """
    if framework_file_finder is not None:
        fw_files = framework_file_finder(path)
    else:
        fw_files = find_source_files(path, list(framework_extensions))
    if not fw_files:
        return

    # grep_files yields one row per matched line, so resolving each filepath
    # to absolute inside the loop would re-do the work N times per file.
    # Build the abs-path map once up front.
    fw_abs = {f: resolve_path(f) for f in fw_files}

    for filepath, _lineno, line in grep_files(
        r"""(?:\bfrom\s+['"]|\bimport\s+['"])""", fw_files
    ):
        importer_abs = fw_abs[filepath]
        cleaned_line = _strip_inline_comments(line)
        for match in _FRAMEWORK_IMPORT_RE.finditer(cleaned_line):
            import_text = match.group(1)
            resolved = spec.resolve_import(import_text, importer_abs, scan_path)
            if resolved is None:
                continue
            if not os.path.isabs(resolved):
                resolved = os.path.normpath(os.path.join(scan_path, resolved))
            if resolved not in file_set:
                continue
            graph[resolved]["importers"].add(importer_abs)


def make_ts_dep_builder(
    spec: TreeSitterLangSpec,
    file_finder: Callable[[Path], list[str]],
    *,
    framework_extensions: tuple[str, ...] | None = None,
    framework_file_finder: Callable[[Path], list[str]] | None = None,
) -> Callable[[Path], dict[str, dict[str, Any]]]:
    """Create a dep graph builder bound to a TreeSitterLangSpec + file finder.

    Returns a callable with signature (path: Path) -> dict,
    matching the contract expected by LangConfig.build_dep_graph.

    When ``framework_extensions`` is provided, framework files under the
    scanned path also contribute importer edges (see ``ts_build_dep_graph``).
    ``framework_file_finder`` lets callers thread the same exclude set used
    for the host language; when omitted, the framework scan honors only
    default exclusions.
    """

    def build(path: Path) -> dict[str, dict[str, Any]]:
        file_list = file_finder(path)
        return ts_build_dep_graph(
            path,
            spec,
            file_list,
            framework_extensions=framework_extensions,
            framework_file_finder=framework_file_finder,
        )

    return build


__all__ = ["make_ts_dep_builder", "ts_build_dep_graph"]

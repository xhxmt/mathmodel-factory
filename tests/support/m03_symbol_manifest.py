"""Deterministic AST source-span regeneration for the M0.3 owner policy."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from factory_core.persisted_dirty_owner_implementation_manifest import (
    PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_SCHEMA,
    PYTHON_AST_SOURCE_SPAN_SCHEMA,
    PersistedDirtyOwnerImplementationManifestV1,
    PythonSymbolSourceSpanV1,
)


SYMBOLS = (
    ("factory_core/dirty.py", "_SOLVER_RECEIPT_RE"),
    ("factory_core/dirty.py", "solver_receipt_job_id"),
    ("factory_core/engine.py", "FactoryEngine._solver_receipt_owner_stage"),
    ("factory_core/engine.py", "FactoryEngine._stage_manifest_delta"),
    ("factory_core/storage.py", "SQLiteStateStore._solver_job_from_row"),
    ("factory_core/storage.py", "SQLiteStateStore.solver_job"),
)


def _find(tree: ast.Module, qualified: str) -> ast.AST:
    parts = qualified.split(".")
    nodes: list[ast.AST] = list(tree.body)
    selected: ast.AST | None = None
    for index, part in enumerate(parts):
        selected = next(
            (
                node
                for node in nodes
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == part
            ),
            None,
        )
        if selected is None and index == 0:
            selected = next(
                (
                    node
                    for node in nodes
                    if isinstance(node, (ast.Assign, ast.AnnAssign))
                    and any(
                        isinstance(target, ast.Name) and target.id == part
                        for target in (
                            node.targets if isinstance(node, ast.Assign) else (node.target,)
                        )
                    )
                ),
                None,
            )
        if selected is None:
            raise ValueError(f"missing source symbol: {qualified}")
        nodes = list(selected.body) if isinstance(selected, ast.ClassDef) else []
    assert selected is not None
    return selected


def rebuild_persisted_dirty_owner_manifest(
    root: Path,
) -> PersistedDirtyOwnerImplementationManifestV1:
    entries = []
    for relative, qualified in SYMBOLS:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"symbol source is not an ordinary file: {relative}")
        data = path.read_bytes()
        tree = ast.parse(data, filename=relative)
        node = _find(tree, qualified)
        if node.end_lineno is None:
            raise ValueError(f"symbol lacks an end position: {qualified}")
        lines = data.splitlines(keepends=True)
        source = b"".join(lines[node.lineno - 1 : node.end_lineno])
        entries.append(
            PythonSymbolSourceSpanV1(
                relative_path=relative,
                qualified_symbol=qualified,
                source_span_schema=PYTHON_AST_SOURCE_SPAN_SCHEMA,
                start_line=node.lineno,
                end_line=node.end_lineno,
                byte_size=len(source),
                source_sha256=hashlib.sha256(source).hexdigest(),
            )
        )
    return PersistedDirtyOwnerImplementationManifestV1(
        schema_version=PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_SCHEMA,
        symbols=tuple(entries),
    )

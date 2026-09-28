"""Structural guards: decision/model engines can only read through a knowledge session."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "fantasy_gm"

# Engines that turn evidence into decisions/estimates. They must never touch storage directly.
ENGINE_MODULES = [
    *sorted((SRC / "organizational_intent").glob("*.py")),
    *sorted((SRC / "leagues").glob("*.py")),
    *sorted((SRC / "draft").glob("*.py")),
    *sorted((SRC / "simulation").glob("*.py")),
    *sorted((SRC / "grading").glob("*.py")),
    SRC / "player_state" / "builder.py",
    SRC / "player_state" / "roles.py",
    SRC / "decisions" / "builder.py",
]
FORBIDDEN_IMPORTS = ("fantasy_gm.persistence",)
FORBIDDEN_NAMES = {
    "known_as_of",
    "InMemoryObservationStore",
    "SqlObservationStore",
    "ObservationStore",
}


@pytest.mark.parametrize("path", ENGINE_MODULES, ids=lambda p: str(p.relative_to(SRC)))
def test_engines_have_no_unfiltered_read_path(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(FORBIDDEN_IMPORTS), node.module
            assert not {a.name for a in node.names} & FORBIDDEN_NAMES, node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(FORBIDDEN_IMPORTS), alias.name
        if isinstance(node, ast.Attribute):
            assert node.attr not in FORBIDDEN_NAMES, f"{path.name}: .{node.attr}"


def test_domain_is_pure() -> None:
    for path in sorted((SRC / "domain").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.startswith(
                    (
                        "fantasy_gm.domain",
                        "__future__",
                        "pydantic",
                        "collections",
                        "datetime",
                        "enum",
                        "typing",
                        "decimal",
                        "itertools",
                        "hashlib",
                        "json",
                        "abc",
                        "threading",
                        "uuid",
                    )
                ), f"{path.name} imports {node.module}"

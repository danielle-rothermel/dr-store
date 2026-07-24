"""dr-store's public contract carries no domain vocabulary.

Guards the explicit exclusions from the design: no Rollout/Whetstone terms,
no workflow/retry/campaign/stage/attempt state, and no speculative
replication or query features. These absence checks fail loudly if such a
concept ever leaks into the public surface.

Matching is on identifier *tokens* (snake_case and CamelCase words), not raw
substrings, so a legitimate word like ``ReferenceValidationError`` -- which
merely contains the letters of "eval" -- is not a false positive.
"""

from __future__ import annotations

import ast
import pathlib
import pkgutil
import re

import dr_store

# Whole-word excluded concepts. Each must not appear as a standalone token in
# any public name, module name, or defined identifier.
FORBIDDEN_WORDS = frozenset(
    {
        "rollout",
        "whetstone",
        "campaign",
        "workflow",
        "retry",
        "stage",
        "attempt",
        "evaluation",
        "eval",
        "graph",
        "optimization",
        "replication",
        "replicate",
        "query",
        "shard",
        "index",
    }
)

# No-mutation / no-speculative-feature entry points on the store surface.
FORBIDDEN_METHODS = frozenset(
    {
        "delete",
        "remove",
        "update",
        "overwrite",
        "clear",
        "rebind",
        "unbind",
        "replace",
        "replicate",
        "sync",
        "query",
        "search",
        "scan",
        "list",
    }
)

_TOKEN_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")


def _tokens(name: str) -> set[str]:
    return {part.lower() for part in _TOKEN_PARTS.findall(name)}


def test_public_names_carry_no_domain_vocabulary() -> None:
    for name in dr_store.__all__:
        leaked = _tokens(name) & FORBIDDEN_WORDS
        assert leaked == set(), f"public name {name!r} leaks {leaked}"


def test_module_names_carry_no_domain_vocabulary() -> None:
    for module in pkgutil.walk_packages(dr_store.__path__, prefix="dr_store."):
        leaf = module.name.rsplit(".", 1)[-1]
        leaked = _tokens(leaf) & FORBIDDEN_WORDS
        assert leaked == set(), f"module {module.name!r} leaks {leaked}"


def test_store_exposes_no_mutation_or_speculative_api() -> None:
    from dr_store import ObjectStore

    public = {n for n in dir(ObjectStore) if not n.startswith("_")}
    assert public & FORBIDDEN_METHODS == set()
    # The only public entry points are the three-part contract plus resolve.
    assert public == {"put", "get", "bind", "resolve"}


def test_defined_identifiers_carry_no_domain_vocabulary() -> None:
    # Parse every module and check class/function/argument identifiers as
    # tokens, so leaked *code* (not exclusion-documenting prose) is caught.
    root = pathlib.Path(dr_store.__path__[0])
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.arg):
                names.append(node.arg)
            for name in names:
                leaked = _tokens(name) & FORBIDDEN_WORDS
                assert leaked == set(), (
                    f"{path.name}: identifier {name!r} leaks {leaked}"
                )

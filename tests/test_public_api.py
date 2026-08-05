"""ObjectStore API shape and heuristic module-vocabulary tripwires."""

from __future__ import annotations

import pkgutil
import re

import dr_store

# These words are a heuristic guard for observable package vocabulary. Naming
# checks cannot prove that implementation or behavior is domain-neutral.
FORBIDDEN_PUBLIC_WORDS = frozenset(
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

_TOKEN_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")


def _tokens(name: str) -> set[str]:
    return {part.lower() for part in _TOKEN_PARTS.findall(name)}


def test_public_module_vocabulary_tripwire() -> None:
    """Heuristically guard observable module names."""
    module_names = [
        ("module", module.name)
        for module in pkgutil.walk_packages(
            dr_store.__path__, prefix="dr_store."
        )
    ]

    for boundary, name in module_names:
        leaf = name.rsplit(".", 1)[-1]
        leaked = _tokens(leaf) & FORBIDDEN_PUBLIC_WORDS
        assert leaked == set(), f"{boundary} {name!r} leaks {leaked}"


def test_object_store_public_surface_is_exact() -> None:
    from dr_store import ObjectStore

    public = {name for name in dir(ObjectStore) if not name.startswith("_")}
    assert public == {"put", "get", "bind", "resolve"}

from __future__ import annotations

import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

import dr_store

DEFS_DIR = Path(__file__).parents[2] / ".defs"
RELATIONSHIP_FIELDS = ("is_a", "part_of")


def _load_toml(name: str) -> dict[str, Any]:
    with (DEFS_DIR / name).open("rb") as file:
        return tomllib.load(file)


def _relationship_edges(
    terms: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    return [
        (term["name"], relationship, target)
        for term in terms
        for relationship in RELATIONSHIP_FIELDS
        for target in term.get(relationship, [])
    ]


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    visited: set[str] = set()
    active_positions: dict[str, int] = {}
    path: list[str] = []

    def visit(name: str) -> list[str] | None:
        visited.add(name)
        active_positions[name] = len(path)
        path.append(name)

        for target in graph[name]:
            if target in active_positions:
                start = active_positions[target]
                return [*path[start:], target]
            if target not in visited:
                cycle = visit(target)
                if cycle is not None:
                    return cycle

        path.pop()
        del active_positions[name]
        return None

    for name in graph:
        if name not in visited:
            cycle = visit(name)
            if cycle is not None:
                return cycle
    return None


def test_term_names_are_unique() -> None:
    terms = _load_toml("terms.toml")["terms"]
    names = [term["name"] for term in terms]

    assert all(name.strip() for name in names)
    assert len(names) == 21
    assert len(names) == len({name.casefold() for name in names})


def test_contract_titles_are_unique() -> None:
    contracts = _load_toml("contracts.toml")["contracts"]
    titles = [contract["title"] for contract in contracts]

    assert all(title.strip() for title in titles)
    assert len(titles) == len({title.casefold() for title in titles})


def test_relationship_targets_exist_and_are_not_self_links() -> None:
    terms = _load_toml("terms.toml")["terms"]
    canonical_terms = {term["name"] for term in terms}
    edges = _relationship_edges(terms)
    missing = [
        f"{source} --{relationship}--> {target}"
        for source, relationship, target in edges
        if target not in canonical_terms
    ]
    self_links = [
        f"{source} --{relationship}--> {target}"
        for source, relationship, target in edges
        if source == target
    ]

    assert not missing, "Missing relationship targets:\n" + "\n".join(missing)
    assert not self_links, "Self-links are invalid:\n" + "\n".join(self_links)


def test_combined_relationship_graph_is_acyclic() -> None:
    terms = _load_toml("terms.toml")["terms"]
    canonical_terms = {term["name"] for term in terms}
    graph = {name: [] for name in canonical_terms}
    for source, _relationship, target in _relationship_edges(terms):
        if target in canonical_terms:
            graph[source].append(target)

    cycle = _find_cycle(graph)
    assert cycle is None, "Relationship cycle: " + " -> ".join(cycle or [])


def test_exported_symbols_are_unique_and_exactly_public() -> None:
    terms = _load_toml("terms.toml")["terms"]
    symbol_terms: dict[str, list[str]] = defaultdict(list)
    for term in terms:
        for symbol in term.get("exported_symbols", []):
            symbol_terms[symbol].append(term["name"])

    duplicates = {
        symbol: names
        for symbol, names in symbol_terms.items()
        if len(names) > 1
    }
    assert not duplicates, (
        f"Exported symbols mapped more than once: {duplicates}"
    )

    mapped_symbols = set(symbol_terms)
    public_symbols = set(dr_store.__all__)
    assert mapped_symbols == public_symbols, (
        "Mappings must exactly cover dr_store.__all__. "
        f"Unmapped: {sorted(public_symbols - mapped_symbols)}. "
        f"Non-public: {sorted(mapped_symbols - public_symbols)}."
    )

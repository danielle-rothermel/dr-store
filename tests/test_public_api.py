from __future__ import annotations

import inspect
import pkgutil
import re

import dr_store

# Naming is only a heuristic for observable vocabulary, not domain neutrality.
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


def test_backend_public_surfaces_are_exact() -> None:
    from dr_store import Backend, MemoryBackend

    expected = {
        "bind",
        "get_binding",
        "get_bound_objects",
        "get_object",
        "put_bound_objects",
        "put_object",
    }
    for backend_type in (Backend, MemoryBackend):
        public = {
            name for name in dir(backend_type) if not name.startswith("_")
        }
        assert public == expected
        assert all(
            inspect.iscoroutinefunction(getattr(backend_type, name))
            for name in expected
        )


def test_postgresql_installer_public_surface_is_exact() -> None:
    from dr_store import install_postgres
    from dr_store.storage_backends import (
        install_postgres as backend_install,
    )
    from dr_store.storage_backends import (
        postgresql,
    )

    assert backend_install is install_postgres
    assert postgresql.__all__ == ["install_postgres"]
    assert inspect.iscoroutinefunction(install_postgres)
    assert list(inspect.signature(install_postgres).parameters) == ["pool"]


def test_record_cache_public_surface_is_exact() -> None:
    from dr_store import RecordCache

    public = {name for name in dir(RecordCache) if not name.startswith("_")}
    assert public == {"get", "get_many", "put", "put_many"}
    assert all(
        inspect.iscoroutinefunction(getattr(RecordCache, name))
        for name in public
    )


def test_sqlite_backend_public_surface_is_exact() -> None:
    from dr_store import SqliteBackend

    public = {name for name in dir(SqliteBackend) if not name.startswith("_")}
    expected = {
        "aclose",
        "bind",
        "get_binding",
        "get_bound_objects",
        "get_object",
        "open",
        "put_bound_objects",
        "put_object",
    }
    assert public == expected
    assert all(
        inspect.iscoroutinefunction(getattr(SqliteBackend, name))
        for name in expected
    )


def test_sqlite_record_cache_public_surface_is_exact() -> None:
    from dr_store import SqliteRecordCache

    public = {
        name for name in dir(SqliteRecordCache) if not name.startswith("_")
    }
    expected = {
        "aclose",
        "get",
        "get_many",
        "open",
        "put",
        "put_many",
    }
    assert public == expected
    assert all(
        inspect.iscoroutinefunction(getattr(SqliteRecordCache, name))
        for name in expected
    )

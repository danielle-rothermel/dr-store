from __future__ import annotations

import inspect
import pkgutil
import re
from dataclasses import fields

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
    assert public == {
        "bind",
        "bind_enlisted",
        "get",
        "get_bound_objects",
        "get_bound_objects_enlisted",
        "get_enlisted",
        "get_many",
        "get_many_enlisted",
        "put",
        "put_enlisted",
        "put_many",
        "put_many_enlisted",
        "resolve",
        "resolve_enlisted",
        "verify_stored_record",
    }


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


def test_postgresql_public_surface_is_exact() -> None:
    from dr_store import (
        POSTGRES_METADATA,
        POSTGRES_SCHEMA_FORMAT,
        PostgresBackend,
        install_postgres,
        install_postgres_sync,
    )
    from dr_store.storage_backends import (
        PostgresBackend as BackendType,
    )
    from dr_store.storage_backends import (
        install_postgres as backend_install,
    )
    from dr_store.storage_backends import (
        install_postgres_sync as backend_install_sync,
    )
    from dr_store.storage_backends import (
        postgresql,
    )

    assert backend_install is install_postgres
    assert backend_install_sync is install_postgres_sync
    assert BackendType is PostgresBackend
    assert postgresql.__all__ == [
        "POSTGRES_METADATA",
        "POSTGRES_SCHEMA_FORMAT",
        "PostgresBackend",
        "install_postgres",
        "install_postgres_sync",
    ]
    assert inspect.iscoroutinefunction(install_postgres)
    assert not inspect.iscoroutinefunction(install_postgres_sync)
    assert list(inspect.signature(install_postgres).parameters) == ["engine"]
    assert list(inspect.signature(install_postgres_sync).parameters) == [
        "engine"
    ]
    assert list(inspect.signature(PostgresBackend).parameters) == ["engine"]
    public = {
        name for name in dir(PostgresBackend) if not name.startswith("_")
    }
    expected = {
        "bind",
        "bind_enlisted",
        "get_binding",
        "get_binding_enlisted",
        "get_bound_objects",
        "get_bound_objects_enlisted",
        "get_object",
        "get_object_enlisted",
        "open",
        "open_sync",
        "put_bound_objects",
        "put_bound_objects_enlisted",
        "put_object",
        "put_object_enlisted",
    }
    assert public == expected
    async_methods = {
        "bind",
        "get_binding",
        "get_bound_objects",
        "get_object",
        "open",
        "put_bound_objects",
        "put_object",
    }
    enlisted_methods = {
        "bind_enlisted",
        "get_binding_enlisted",
        "get_bound_objects_enlisted",
        "get_object_enlisted",
        "put_bound_objects_enlisted",
        "put_object_enlisted",
    }
    sync_methods = {"open_sync", *enlisted_methods}
    assert all(
        inspect.iscoroutinefunction(getattr(PostgresBackend, name))
        for name in async_methods
    )
    assert all(
        not inspect.iscoroutinefunction(getattr(PostgresBackend, name))
        for name in sync_methods
    )
    open_parameters = list(inspect.signature(PostgresBackend.open).parameters)
    assert open_parameters == ["engine", "batch_chunk_size"]
    open_sync_parameters = list(
        inspect.signature(PostgresBackend.open_sync).parameters
    )
    assert open_sync_parameters == ["engine", "batch_chunk_size"]
    assert POSTGRES_SCHEMA_FORMAT == "dr-store-postgresql-v1"
    assert POSTGRES_METADATA is postgresql.POSTGRES_METADATA


def test_sqlite_backend_open_exposes_busy_timeout_knob() -> None:
    from dr_store import SqliteBackend

    assert (
        "busy_timeout_ms" in inspect.signature(SqliteBackend.open).parameters
    )


def test_sqlite_record_cache_open_exposes_busy_timeout_knob() -> None:
    from dr_store import SqliteRecordCache

    assert (
        "busy_timeout_ms"
        in inspect.signature(SqliteRecordCache.open).parameters
    )


def test_public_exports_include_object_reference_wire_format() -> None:
    import dr_store

    assert dr_store.OBJECT_REFERENCE_PREFIX == "dr-store-object:v1"
    assert callable(dr_store.format_object_reference)
    assert callable(dr_store.parse_object_reference)


def test_object_store_enlisted_methods_are_sync() -> None:
    from dr_store import ObjectStore

    for name in (
        "put_enlisted",
        "bind_enlisted",
        "put_many_enlisted",
        "get_bound_objects_enlisted",
        "get_enlisted",
        "get_many_enlisted",
        "resolve_enlisted",
    ):
        assert not inspect.iscoroutinefunction(getattr(ObjectStore, name))


def test_record_cache_public_surface_is_exact() -> None:
    from dr_store import RecordCache

    public = {name for name in dir(RecordCache) if not name.startswith("_")}
    assert public == {"get", "get_many", "put", "put_many", "stats"}
    assert all(
        inspect.iscoroutinefunction(getattr(RecordCache, name))
        for name in public
        if name != "stats"
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
        "stats",
    }
    assert public == expected
    assert all(
        inspect.iscoroutinefunction(getattr(SqliteRecordCache, name))
        for name in expected
        if name != "stats"
    )


def test_sidecar_hash_public_surface_is_exact() -> None:
    from dr_store import DocumentDirectory, SidecarSummary

    assert tuple(field.name for field in fields(SidecarSummary)) == (
        "head_length",
        "tail_length",
        "produced",
        "dropped",
        "sidecar_hash",
    )
    assert list(
        inspect.signature(DocumentDirectory.verify_sidecar).parameters
    ) == [
        "self",
        "name",
        "expected_sidecar_hash",
        "expected_head_length",
        "expected_tail_length",
    ]

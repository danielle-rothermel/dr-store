from __future__ import annotations

import pkgutil
import re
from typing import TYPE_CHECKING

import dr_store

if TYPE_CHECKING:
    from pathlib import Path

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


def test_sqlite_record_cache_public_surface_is_exact() -> None:
    from dr_store import RecordCache, SqliteRecordCache

    assert issubclass(SqliteRecordCache, RecordCache)
    public = {
        name for name in dir(SqliteRecordCache) if not name.startswith("_")
    }
    assert public == {"close", "get", "put"}
    assert "__enter__" in SqliteRecordCache.__dict__
    assert "__exit__" in SqliteRecordCache.__dict__


def test_canonical_json_file_root_surface_and_behavior_are_exact(
    tmp_path: Path,
) -> None:
    from dr_store import CanonicalJsonFile
    from dr_store.document_file import CanonicalJsonFile as PackageFile

    assert CanonicalJsonFile is PackageFile
    public = {
        name for name in dir(CanonicalJsonFile) if not name.startswith("_")
    }
    assert public == {"path", "publish", "read"}

    document_file = CanonicalJsonFile(
        tmp_path,
        "document.json",
        max_bytes=1 << 12,
    )
    assert document_file.path == tmp_path / "document.json"
    document_file.publish({"b": 2, "a": 1})
    assert document_file.read() == {"a": 1, "b": 2}


def test_document_file_errors_are_root_exports_with_public_context(
    tmp_path: Path,
) -> None:
    from dr_store import (
        DocumentFileError,
        DocumentPublishError,
        DocumentReadError,
        PublicationStage,
    )
    from dr_store import document_file as package

    assert DocumentFileError is package.DocumentFileError
    assert DocumentPublishError is package.DocumentPublishError
    assert DocumentReadError is package.DocumentReadError
    assert PublicationStage is package.PublicationStage
    assert issubclass(DocumentPublishError, DocumentFileError)
    assert issubclass(DocumentReadError, DocumentFileError)

    path = tmp_path / "document.json"
    publish_error = DocumentPublishError(
        path,
        PublicationStage.REPLACE_TARGET,
        replacement_completed=False,
    )
    assert publish_error.path == path
    assert publish_error.stage is PublicationStage.REPLACE_TARGET
    assert publish_error.replacement_completed is False

    read_error = DocumentReadError(path)
    assert read_error.path == path


def test_publication_stage_members_and_values_are_exact() -> None:
    from dr_store import PublicationStage

    assert [(member.name, member.value) for member in PublicationStage] == [
        ("ENCODE", "encode"),
        ("CREATE_TEMP", "create_temp"),
        ("WRITE_TEMP", "write_temp"),
        ("FLUSH_TEMP", "flush_temp"),
        ("REPLACE_TARGET", "replace_target"),
        ("FLUSH_DIRECTORY", "flush_directory"),
    ]

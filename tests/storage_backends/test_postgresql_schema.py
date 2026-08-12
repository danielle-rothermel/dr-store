from __future__ import annotations

from dr_store import POSTGRES_METADATA


def test_postgres_metadata_table_layout_is_pinned() -> None:
    tables = {
        table.name: tuple(column.name for column in table.columns)
        for table in POSTGRES_METADATA.sorted_tables
    }
    assert tables == {
        "schema_format": ("singleton", "format"),
        "objects": ("content_hash", "schema", "canonical"),
        "bindings": ("key", "schema", "content_hash"),
    }
    assert POSTGRES_METADATA.schema == "dr_store"

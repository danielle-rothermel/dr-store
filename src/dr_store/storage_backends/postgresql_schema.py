from __future__ import annotations

from typing import Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
)

POSTGRES_SCHEMA: Final = "dr_store"

# Persisted schema-format contract. A changed literal is a new format.
POSTGRES_SCHEMA_FORMAT: Final = "dr-store-postgresql-v1"

_HASH_CHECK = (
    "octet_length(content_hash) = 64 AND "
    "translate(content_hash, '0123456789abcdef', '') = ''"
)

POSTGRES_METADATA = MetaData(schema=POSTGRES_SCHEMA)

_ucs_text = Text(collation="ucs_basic")

Table(
    "schema_format",
    POSTGRES_METADATA,
    Column("singleton", Boolean, primary_key=True),
    Column("format", _ucs_text, nullable=False),
    CheckConstraint("singleton", name="schema_format_singleton"),
)

Table(
    "objects",
    POSTGRES_METADATA,
    Column("content_hash", _ucs_text, nullable=False),
    Column("schema", _ucs_text, nullable=False),
    Column("canonical", _ucs_text, nullable=False),
    CheckConstraint(_HASH_CHECK, name="objects_content_hash_lowercase_hex"),
    PrimaryKeyConstraint("content_hash", "schema"),
)

Table(
    "bindings",
    POSTGRES_METADATA,
    Column("key", _ucs_text, primary_key=True),
    Column("schema", _ucs_text, nullable=False),
    Column("content_hash", _ucs_text, nullable=False),
    CheckConstraint(_HASH_CHECK, name="bindings_content_hash_lowercase_hex"),
)

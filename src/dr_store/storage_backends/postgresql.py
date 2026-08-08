from __future__ import annotations

import asyncpg

__all__ = ["install_postgres"]

_MINIMUM_POSTGRES_MAJOR = 16
_MAXIMUM_POSTGRES_MAJOR = 18

_DATABASE_REQUIREMENTS_SQL = """
SELECT
    pg_catalog.current_setting('server_version_num')::pg_catalog.int4
        AS server_version_num,
    pg_catalog.current_setting('server_encoding') AS server_encoding
"""

_INSTALL_SQL = """
CREATE SCHEMA dr_store;

CREATE TABLE dr_store.objects (
    content_hash pg_catalog.text COLLATE pg_catalog.ucs_basic NOT NULL,
    schema pg_catalog.text COLLATE pg_catalog.ucs_basic NOT NULL,
    canonical pg_catalog.text COLLATE pg_catalog.ucs_basic NOT NULL,
    CONSTRAINT objects_content_hash_lowercase_hex CHECK (
        pg_catalog.octet_length(content_hash) = 64
        AND pg_catalog.translate(
            content_hash,
            '0123456789abcdef',
            ''
        ) = ''
    ),
    PRIMARY KEY (content_hash, schema)
);

CREATE TABLE dr_store.bindings (
    key pg_catalog.text COLLATE pg_catalog.ucs_basic PRIMARY KEY,
    schema pg_catalog.text COLLATE pg_catalog.ucs_basic NOT NULL,
    content_hash pg_catalog.text COLLATE pg_catalog.ucs_basic NOT NULL,
    CONSTRAINT bindings_content_hash_lowercase_hex CHECK (
        pg_catalog.octet_length(content_hash) = 64
        AND pg_catalog.translate(
            content_hash,
            '0123456789abcdef',
            ''
        ) = ''
    )
);
"""


def _validate_database(*, version_num: int, server_encoding: str) -> None:
    major = version_num // 10_000
    if not _MINIMUM_POSTGRES_MAJOR <= major <= _MAXIMUM_POSTGRES_MAJOR:
        raise RuntimeError("PostgreSQL 16 through 18 is required")
    if server_encoding != "UTF8":
        raise RuntimeError("PostgreSQL server encoding must be UTF-8")


async def install_postgres(pool: asyncpg.Pool) -> None:
    """Install the fixed ``dr_store`` schema into an empty namespace.

    The caller owns ``pool``. This operation acquires and releases one
    connection without closing the pool.
    """
    if not isinstance(pool, asyncpg.Pool):
        raise TypeError("pool must be an asyncpg.Pool")

    async with (
        pool.acquire() as connection,
        connection.transaction(),
    ):
        requirements = await connection.fetchrow(_DATABASE_REQUIREMENTS_SQL)
        assert requirements is not None
        _validate_database(
            version_num=requirements["server_version_num"],
            server_encoding=requirements["server_encoding"],
        )
        await connection.execute(_INSTALL_SQL)

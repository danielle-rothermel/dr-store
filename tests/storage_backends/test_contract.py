from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from dr_store import (
    BindOutcome,
    BoundObjectRow,
    BoundObjectWrite,
    ObjectConflictError,
    PutOutcome,
    ReferenceValidationError,
)

if TYPE_CHECKING:
    from dr_store import Backend

SCHEMA = "example.record"
OTHER_SCHEMA = "other.record"
CONTENT_HASH = "a" * 64
OTHER_HASH = "b" * 64
CANONICAL = '{"value":"first"}'
COMPETING_CANONICAL = '{"value":"second"}'
KEY = "caller-owned-key"
CONTENDERS = 8


async def test_put_absent_replay_and_competing_value(
    backend: Backend,
) -> None:
    assert (
        await backend.get_object(
            schema=SCHEMA,
            content_hash=CONTENT_HASH,
        )
        is None
    )
    assert await backend.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    ) == PutOutcome(
        inserted=True,
        stored_canonical=CANONICAL,
    )
    assert await backend.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    ) == PutOutcome(
        inserted=False,
        stored_canonical=CANONICAL,
    )
    assert await backend.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=COMPETING_CANONICAL,
    ) == PutOutcome(
        inserted=False,
        stored_canonical=CANONICAL,
    )


async def test_get_prefers_exact_then_alternate_schema(
    backend: Backend,
) -> None:
    await backend.put_object(
        schema=OTHER_SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=COMPETING_CANONICAL,
    )
    assert await backend.get_object(
        schema=SCHEMA, content_hash=CONTENT_HASH
    ) == (OTHER_SCHEMA, COMPETING_CANONICAL)
    await backend.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    )
    assert await backend.get_object(
        schema=SCHEMA, content_hash=CONTENT_HASH
    ) == (SCHEMA, CANONICAL)


async def test_bind_absent_replay_and_competing_reference(
    backend: Backend,
) -> None:
    assert await backend.get_binding(key=KEY) is None
    assert await backend.bind(
        key=KEY, schema=SCHEMA, content_hash=CONTENT_HASH
    ) == BindOutcome(
        bound=True,
        existing_schema=SCHEMA,
        existing_content_hash=CONTENT_HASH,
    )
    assert await backend.bind(
        key=KEY, schema=SCHEMA, content_hash=CONTENT_HASH
    ) == BindOutcome(
        bound=False,
        existing_schema=SCHEMA,
        existing_content_hash=CONTENT_HASH,
    )
    assert await backend.bind(
        key=KEY, schema=OTHER_SCHEMA, content_hash=OTHER_HASH
    ) == BindOutcome(
        bound=False,
        existing_schema=SCHEMA,
        existing_content_hash=CONTENT_HASH,
    )


async def test_batch_put_get_and_conflict_rollback(
    backend: Backend,
) -> None:
    first = BoundObjectWrite(KEY, SCHEMA, CONTENT_HASH, CANONICAL)
    missing_object_key = "missing-object"
    assert await backend.put_bound_objects(entries=(first,)) == {
        KEY: BindOutcome(
            bound=True,
            existing_schema=SCHEMA,
            existing_content_hash=CONTENT_HASH,
        )
    }
    await backend.bind(
        key=missing_object_key,
        schema=OTHER_SCHEMA,
        content_hash=OTHER_HASH,
    )
    assert await backend.get_bound_objects(
        keys=(KEY, "unbound", missing_object_key)
    ) == {
        KEY: BoundObjectRow(SCHEMA, CONTENT_HASH, CANONICAL),
        missing_object_key: BoundObjectRow(OTHER_SCHEMA, OTHER_HASH, None),
    }

    colliding_hash = "c" * 64
    await backend.put_object(
        schema=SCHEMA,
        content_hash=colliding_hash,
        canonical=COMPETING_CANONICAL,
    )
    with pytest.raises(ObjectConflictError):
        await backend.put_bound_objects(
            entries=(
                BoundObjectWrite(
                    "new-key", OTHER_SCHEMA, OTHER_HASH, CANONICAL
                ),
                BoundObjectWrite(
                    "collision", SCHEMA, colliding_hash, CANONICAL
                ),
            )
        )
    assert await backend.get_binding(key="new-key") is None
    assert (
        await backend.get_object(schema=OTHER_SCHEMA, content_hash=OTHER_HASH)
        is None
    )


@pytest.mark.parametrize(
    "value",
    ["\0", "schema\0tail", "\ud800", "head\udffftail"],
)
async def test_every_schema_path_rejects_invalid_text(
    backend: Backend, value: str
) -> None:
    operations = (
        lambda: backend.put_object(
            schema=value, content_hash=CONTENT_HASH, canonical=CANONICAL
        ),
        lambda: backend.get_object(schema=value, content_hash=CONTENT_HASH),
        lambda: backend.bind(key=KEY, schema=value, content_hash=CONTENT_HASH),
        lambda: backend.put_bound_objects(
            entries=(BoundObjectWrite(KEY, value, CONTENT_HASH, CANONICAL),)
        ),
    )
    for operation in operations:
        with pytest.raises(ReferenceValidationError):
            await operation()


@pytest.mark.parametrize("value", ["\0", "key\0tail", "\ud800"])
async def test_every_key_path_rejects_invalid_text(
    backend: Backend, value: str
) -> None:
    operations = (
        lambda: backend.bind(
            key=value, schema=SCHEMA, content_hash=CONTENT_HASH
        ),
        lambda: backend.get_binding(key=value),
        lambda: backend.get_bound_objects(keys=(value,)),
        lambda: backend.put_bound_objects(
            entries=(BoundObjectWrite(value, SCHEMA, CONTENT_HASH, CANONICAL),)
        ),
    )
    for operation in operations:
        with pytest.raises(ReferenceValidationError):
            await operation()


@pytest.mark.parametrize(
    "value",
    ["not-a-hash", "A" * 64, "a" * 63, "g" * 64],
)
async def test_every_content_hash_path_rejects_malformed_values(
    backend: Backend, value: str
) -> None:
    operations = (
        lambda: backend.put_object(
            schema=SCHEMA, content_hash=value, canonical=CANONICAL
        ),
        lambda: backend.get_object(schema=SCHEMA, content_hash=value),
        lambda: backend.bind(key=KEY, schema=SCHEMA, content_hash=value),
        lambda: backend.put_bound_objects(
            entries=(BoundObjectWrite(KEY, SCHEMA, value, CANONICAL),)
        ),
    )
    for operation in operations:
        with pytest.raises(ReferenceValidationError):
            await operation()


async def test_empty_key_is_valid(backend: Backend) -> None:
    assert (
        await backend.bind(key="", schema=SCHEMA, content_hash=CONTENT_HASH)
    ).bound
    assert await backend.get_binding(key="") == (SCHEMA, CONTENT_HASH)


@pytest.mark.parametrize(
    "canonicals",
    [
        pytest.param([CANONICAL] * CONTENDERS, id="same-value"),
        pytest.param(
            [f'{{"contender":{index}}}' for index in range(CONTENDERS)],
            id="competing-values",
        ),
    ],
)
async def test_put_contention_has_one_correlated_winner(
    backend: Backend, canonicals: list[str]
) -> None:
    start = asyncio.Event()

    async def contend(canonical: str) -> tuple[str, PutOutcome]:
        await start.wait()
        return canonical, await backend.put_object(
            schema=SCHEMA,
            content_hash=CONTENT_HASH,
            canonical=canonical,
        )

    tasks = [asyncio.create_task(contend(value)) for value in canonicals]
    start.set()
    results = await asyncio.gather(*tasks)
    assert sum(outcome.inserted for _, outcome in results) == 1
    winner = await backend.get_object(schema=SCHEMA, content_hash=CONTENT_HASH)
    assert winner is not None
    for contender, outcome in results:
        assert outcome.stored_canonical == winner[1]
        if outcome.inserted:
            assert contender == winner[1]


async def test_bind_contention_has_one_correlated_winner(
    backend: Backend,
) -> None:
    references = [
        (f"schema.{index}", f"{index:x}" * 64) for index in range(CONTENDERS)
    ]
    start = asyncio.Event()

    async def contend(
        reference: tuple[str, str],
    ) -> tuple[tuple[str, str], BindOutcome]:
        await start.wait()
        return reference, await backend.bind(
            key=KEY, schema=reference[0], content_hash=reference[1]
        )

    tasks = [asyncio.create_task(contend(value)) for value in references]
    start.set()
    results = await asyncio.gather(*tasks)
    assert sum(outcome.bound for _, outcome in results) == 1
    winner = await backend.get_binding(key=KEY)
    assert winner in references
    for contender, outcome in results:
        assert (
            outcome.existing_schema,
            outcome.existing_content_hash,
        ) == winner
        if outcome.bound:
            assert contender == winner

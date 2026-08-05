"""Direct semantic conformance shared by Memory and SQLite backends."""

from __future__ import annotations

import concurrent.futures
import threading
from typing import TYPE_CHECKING

import pytest

from dr_store import BindOutcome, PutOutcome

if TYPE_CHECKING:
    from dr_store.storage_backends.contract import Backend

SCHEMA = "example.record"
OTHER_SCHEMA = "other.record"
CONTENT_HASH = "a" * 64
OTHER_HASH = "b" * 64
CANONICAL = '{"value":"first"}'
COMPETING_CANONICAL = '{"value":"second"}'
KEY = "caller-owned-key"
CONTENDERS = 8
WATCHDOG_SECONDS = 10


def test_put_absent_replay_and_competing_value(
    backend: Backend,
) -> None:
    assert (
        backend.get_object(
            schema=SCHEMA,
            content_hash=CONTENT_HASH,
        )
        is None
    )

    assert backend.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    ) == PutOutcome(
        inserted=True,
        stored_schema=SCHEMA,
        stored_canonical=CANONICAL,
    )
    assert backend.get_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ) == (SCHEMA, CANONICAL)

    assert backend.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    ) == PutOutcome(
        inserted=False,
        stored_schema=SCHEMA,
        stored_canonical=CANONICAL,
    )
    assert backend.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=COMPETING_CANONICAL,
    ) == PutOutcome(
        inserted=False,
        stored_schema=SCHEMA,
        stored_canonical=CANONICAL,
    )
    # The competing write reports, and leaves, the original winner.
    assert backend.get_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ) == (SCHEMA, CANONICAL)


def test_get_prefers_exact_row_then_falls_back_to_one_alternate(
    backend: Backend,
) -> None:
    backend.put_object(
        schema=OTHER_SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=COMPETING_CANONICAL,
    )
    assert backend.get_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ) == (OTHER_SCHEMA, COMPETING_CANONICAL)

    backend.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    )
    assert backend.get_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ) == (SCHEMA, CANONICAL)


def test_bind_absent_replay_and_competing_reference(
    backend: Backend,
) -> None:
    assert backend.get_binding(key=KEY) is None

    assert backend.bind(
        key=KEY,
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ) == BindOutcome(
        bound=True,
        existing_schema=SCHEMA,
        existing_content_hash=CONTENT_HASH,
    )
    assert backend.get_binding(key=KEY) == (SCHEMA, CONTENT_HASH)

    assert backend.bind(
        key=KEY,
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ) == BindOutcome(
        bound=False,
        existing_schema=SCHEMA,
        existing_content_hash=CONTENT_HASH,
    )
    assert backend.bind(
        key=KEY,
        schema=OTHER_SCHEMA,
        content_hash=OTHER_HASH,
    ) == BindOutcome(
        bound=False,
        existing_schema=SCHEMA,
        existing_content_hash=CONTENT_HASH,
    )
    # The competing bind reports, and leaves, the original winner.
    assert backend.get_binding(key=KEY) == (SCHEMA, CONTENT_HASH)


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
def test_same_process_put_contention_has_one_correlated_winner(
    backend: Backend,
    canonicals: list[str],
) -> None:
    ready = threading.Barrier(CONTENDERS)

    def contend(canonical: str) -> tuple[str, PutOutcome]:
        ready.wait(timeout=WATCHDOG_SECONDS)
        return (
            canonical,
            backend.put_object(
                schema=SCHEMA,
                content_hash=CONTENT_HASH,
                canonical=canonical,
            ),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONTENDERS) as pool:
        futures = [pool.submit(contend, value) for value in canonicals]
        results = [
            future.result(timeout=WATCHDOG_SECONDS) for future in futures
        ]

    assert sum(outcome.inserted for _, outcome in results) == 1
    winner = backend.get_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    )
    assert winner is not None
    winner_schema, winner_canonical = winner
    assert winner_schema == SCHEMA
    assert winner_canonical in canonicals
    for contender, outcome in results:
        assert outcome.stored_schema == winner_schema
        assert outcome.stored_canonical == winner_canonical
        if outcome.inserted:
            assert contender == winner_canonical


@pytest.mark.parametrize(
    "references",
    [
        pytest.param(
            [(SCHEMA, CONTENT_HASH)] * CONTENDERS,
            id="same-reference",
        ),
        pytest.param(
            [
                (f"schema.{index}", f"{index:x}" * 64)
                for index in range(CONTENDERS)
            ],
            id="competing-references",
        ),
    ],
)
def test_same_process_bind_contention_has_one_correlated_winner(
    backend: Backend,
    references: list[tuple[str, str]],
) -> None:
    ready = threading.Barrier(CONTENDERS)

    def contend(
        reference: tuple[str, str],
    ) -> tuple[tuple[str, str], BindOutcome]:
        schema, content_hash = reference
        ready.wait(timeout=WATCHDOG_SECONDS)
        return (
            reference,
            backend.bind(
                key=KEY,
                schema=schema,
                content_hash=content_hash,
            ),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONTENDERS) as pool:
        futures = [pool.submit(contend, reference) for reference in references]
        results = [
            future.result(timeout=WATCHDOG_SECONDS) for future in futures
        ]

    assert sum(outcome.bound for _, outcome in results) == 1
    winner = backend.get_binding(key=KEY)
    assert winner in references
    for contender, outcome in results:
        assert (
            outcome.existing_schema,
            outcome.existing_content_hash,
        ) == winner
        if outcome.bound:
            assert contender == winner
